package org.tenforward.radio

import android.app.PendingIntent
import android.content.Intent
import android.os.Handler
import android.os.Looper
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * The radio itself: one ExoPlayer, one media session (that is the lock screen and the notification), and a
 * small loop that asks the server for the next song on the channel you are tuned to.
 */
class PlaybackService : MediaSessionService() {

    companion object {
        @Volatile
        var instance: PlaybackService? = null

        /**
         * Songs already heard, kept on the phone. One instance for the whole process - SimpleCache
         * locks its folder, so a second one over the same directory throws.
         *
         * This is what makes the radio survive a tunnel: a song already pulled down plays from
         * here with no network at all, and a song half pulled down resumes from where it got to
         * instead of starting again.
         */
        private var cacheRef: SimpleCache? = null

        @Synchronized
        fun cache(ctx: android.content.Context): SimpleCache {
            cacheRef?.let { return it }
            val dir = File(ctx.cacheDir, "songs")
            // the 2-argument constructor is deprecated: without an index database SimpleCache has to
            // walk the whole folder on every start, which on 512 MB of songs is not free
            val c = SimpleCache(dir, LeastRecentlyUsedCacheEvictor(CACHE_BYTES), StandaloneDatabaseProvider(ctx))
            cacheRef = c
            return c
        }

        const val CACHE_BYTES = 512L * 1024 * 1024      // about 150 songs at 3.5 MB
    }

    private lateinit var player: ExoPlayer
    private var session: MediaSession? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val songs = ConcurrentHashMap<String, Song>()   // media id -> song (written on both threads)
    private var markPlayed: Runnable? = null
    private var sleepRun: Runnable? = null
    private var retryRun: Runnable? = null
    @Volatile private var fetching = false
    @Volatile private var retried = false

    override fun onCreate() {
        super.onCreate()
        Prefs.init(this)
        ErrorLog.init(this)
        instance = this
        // Everything the player reads goes through the cache on the way past, so a song heard
        // once is on the phone. The buffer is also far deeper than the default ~50 s: in a car
        // the question is not "how smooth is this" but "how long a dead zone can it ride out".
        val http = DefaultHttpDataSource.Factory()
            .setConnectTimeoutMs(8000)
            .setReadTimeoutMs(20000)
            .setAllowCrossProtocolRedirects(true)
        val cached = CacheDataSource.Factory()
            .setCache(cache(this))
            .setUpstreamDataSourceFactory(http)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
        player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(cached))
            .setLoadControl(
                DefaultLoadControl.Builder()
                    .setBufferDurationsMs(60_000, 600_000, 2_500, 5_000)
                    .build()
            )
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .setUsage(C.USAGE_MEDIA)
                    .build(),
                true,
            )
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        player.addListener(object : Player.Listener {
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) = onItemStart(mediaItem)

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                Radio.playing = isPlaying
                Radio.changed()
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == Player.STATE_ENDED) endOfQueue()
                if (playbackState == Player.STATE_READY) {
                    retried = false
                    prefetch()
                }
            }

            override fun onPlayerError(error: PlaybackException) = onError(error)
        })
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        session = MediaSession.Builder(this, player).setSessionActivity(open).build()
        Watch.begin(this)   // music playing with the screen off is exactly when a channel finishes building
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        if (!player.isPlaying) stopSelf()
    }

    override fun onDestroy() {
        Watch.end()
        markPlayed?.let { main.removeCallbacks(it) }
        sleepRun?.let { main.removeCallbacks(it) }
        session?.release()
        session = null
        player.release()
        instance = null
        super.onDestroy()
    }

    // ---------------------------------------------------------------- the dial
    fun tune(station: Station) {
        cancelRetry()   // a dry channel's retry must not fire onto the channel you just moved to
        Radio.stationId = station.id
        Radio.stationName = station.name
        Radio.status = "Tuning to ${station.name}…"
        Prefs.lastStation = station.id
        Radio.changed()
        io.execute {
            try {
                ensureBase()
                val song = Api.next(station.id, null)
                if (song == null) {
                    // The browser asks the server to make one when a channel is dry; the app never
                    // did, so the client doing nearly all the listening was the one that never fed
                    // the generator. Ask, say so, and come back for it.
                    askForOne(station.id)
                    Radio.status = "Nothing ready on ${station.name} yet - making one"
                    Radio.changed()
                    val r = Runnable { endOfQueue() }
                    retryRun = r
                    main.postDelayed(r, 20_000L)
                    return@execute
                }
                songs.clear()
                val item = item(song)
                main.post {
                    player.clearMediaItems()
                    player.setMediaItem(item)
                    player.prepare()
                    player.play()
                }
            } catch (e: Exception) {
                fail(e)
            }
        }
    }

    /**
     * A song you picked by hand. It starts now, and the radio stays on the channel it came from so
     * the station carries on behind it - picking one song does not end the radio.
     *
     * Anything the radio had already loaded ahead is thrown away first: that was its guess at what
     * you wanted next, and you have just said otherwise.
     */
    fun playNow(song: Song, stationId: String?, stationName: String?) {
        cancelRetry()   // you have said what you want to hear; nothing pending gets to override it
        if (!stationId.isNullOrEmpty()) {
            Radio.stationId = stationId
            Radio.stationName = stationName ?: stationId
            Prefs.lastStation = stationId
        }
        main.post {
            dropAhead()
            val item = item(song)
            player.addMediaItem(item)
            player.seekTo(player.mediaItemCount - 1, 0L)
            player.prepare()
            player.play()
        }
    }

    /**
     * Something new went into the line. Throw away the song the radio had loaded ahead and ask
     * again, so what was just lined up is genuinely the next thing heard rather than the one after.
     */
    fun requeue() {
        main.post {
            dropAhead()
            prefetch()
        }
    }

    private fun cancelRetry() {
        retryRun?.let { main.removeCallbacks(it) }
        retryRun = null
    }

    /** Where the song has got to, and how long it is, in milliseconds. (0, 0) when nothing is on. */
    fun position(): Pair<Long, Long> {
        val d = player.duration
        return player.currentPosition.coerceAtLeast(0L) to (if (d > 0) d else 0L)
    }

    /** Move within the song that is playing. */
    fun seekWithin(ms: Long) {
        main.post { player.seekTo(ms.coerceAtLeast(0L)) }
    }

    /** Drop everything loaded after the song playing now. Safe when nothing is loaded at all. */
    private fun dropAhead() {
        val from = (player.currentMediaItemIndex + 1).coerceAtLeast(0)
        if (player.mediaItemCount > from) player.removeMediaItems(from, player.mediaItemCount)
    }

    fun skip() {
        main.post {
            if (player.hasNextMediaItem()) {
                player.seekToNextMediaItem()
                player.play()
            } else {
                endOfQueue()
            }
        }
    }

    /**
     * Back a song, the way every music player does it: near the start of a song it goes to the one before,
     * later in a song it goes back to the beginning of this one. Songs already heard stay in the list, so
     * the lock screen's back button works too.
     */
    fun previous() {
        main.post {
            if (player.currentPosition > 5_000L || !player.hasPreviousMediaItem()) {
                player.seekTo(0L)
                player.play()
            } else {
                player.seekToPreviousMediaItem()
                player.play()
            }
        }
    }

    fun playPause() {
        main.post {
            if (player.isPlaying) player.pause()
            else if (player.mediaItemCount > 0) player.play()
        }
    }

    fun heart() {
        val song = Radio.song ?: return
        io.execute {
            try {
                val liked = Api.like(song.id)
                val updated = song.copy(liked = liked)
                songs[song.id] = updated
                Radio.song = updated
                Radio.liked = liked
                Radio.status = if (liked) "Kept: saved on the server" else "Not kept any more"
            } catch (e: Exception) {
                fail(e)
                return@execute
            }
            Radio.changed()
        }
    }

    /** minutes: -1 stop after this song, 0 off, otherwise stop in that many minutes. */
    fun sleep(minutes: Int) {
        sleepRun?.let { main.removeCallbacks(it) }
        sleepRun = null
        Radio.sleepAt = 0L
        Radio.stopAfterSong = false
        when {
            minutes < 0 -> {
                Radio.stopAfterSong = true
                Radio.status = "Stopping after this song"
            }
            minutes > 0 -> {
                val ms = minutes * 60_000L
                val r = Runnable {
                    player.pause()
                    Radio.sleepAt = 0L
                    Radio.status = "Sleep timer: stopped"
                    Radio.changed()
                }
                sleepRun = r
                Radio.sleepAt = System.currentTimeMillis() + ms
                main.postDelayed(r, ms)
                Radio.status = "Stopping in $minutes minutes"
            }
            else -> Radio.status = "Sleep timer off"
        }
        Radio.changed()
    }

    // ---------------------------------------------------------------- inside
    private fun item(song: Song): MediaItem {
        songs[song.id] = song
        return MediaItem.Builder()
            .setUri(Api.mediaUrl(song))
            .setMediaId(song.id)
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(song.title)
                    // a cover belongs on the lock screen too - that is the only place the song is
                    // named when the phone is in a pocket or a dash mount
                    .setArtist("Ten Forward · " + Radio.stationName)
                    .setAlbumTitle(song.coverLine() ?: song.style.take(70))
                    .build()
            )
            .build()
    }

    private fun onItemStart(mediaItem: MediaItem?) {
        markPlayed?.let { main.removeCallbacks(it) }
        val song = mediaItem?.mediaId?.let { songs[it] }
        Radio.song = song
        Radio.liked = song?.liked ?: false
        if (song != null) Radio.status = ""
        Radio.changed()
        if (song != null) {
            val r = Runnable {
                io.execute {
                    try {
                        Api.played(song.id, Radio.stationId)
                    } catch (e: Exception) {
                        // a missed play count is not worth telling anybody about
                    }
                }
            }
            markPlayed = r
            main.postDelayed(r, 10_000L)
        }
        prefetch()
    }

    /** Keep one song loaded behind the current one so the notification's next button always works. */
    private fun prefetch() {
        val stationId = Radio.stationId ?: return
        main.post {
            if (fetching) return@post
            // two ahead rather than one: each is already being pulled into the cache, so this is
            // how far out of signal the radio can go and still have something to play
            if (player.mediaItemCount - (player.currentMediaItemIndex + 1) >= 2) return@post
            fetching = true
            val exclude = Radio.song?.id
            io.execute {
                try {
                    ensureBase()
                    val song = Api.next(stationId, exclude)
                    if (song != null) {
                        val item = item(song)
                        main.post {
                            player.addMediaItem(item)
                            trimHistory()
                        }
                    }
                } catch (e: Exception) {
                    // the next song can wait; the end of this one asks again
                } finally {
                    fetching = false
                }
            }
        }
    }

    private fun endOfQueue() {
        cancelRetry()
        if (Radio.stopAfterSong) {
            Radio.stopAfterSong = false
            Radio.status = "Sleep timer: stopped"
            Radio.changed()
            return
        }
        val stationId = Radio.stationId ?: return
        val exclude = Radio.song?.id
        io.execute {
            try {
                ensureBase()
                val song = Api.next(stationId, exclude)
                if (song == null) {
                    askForOne(stationId)
                    Radio.status = "Waiting for the next song to finish being made…"
                    Radio.changed()
                    val r = Runnable { endOfQueue() }
                    retryRun = r
                    main.postDelayed(r, 20_000L)
                    return@execute
                }
                val item = item(song)
                main.post {
                    // the songs already heard stay in the list so Back works; only the very old ones go
                    player.addMediaItem(item)
                    player.seekTo(player.mediaItemCount - 1, 0L)
                    player.prepare()
                    player.play()
                    trimHistory()
                }
            } catch (e: Exception) {
                // Offline this is where the music used to simply end: fail() set a status string and
                // scheduled nothing, so the radio stayed dead until somebody picked the phone up.
                // Anything already in the cache can still play, so keep asking.
                fail(e)
                val r = Runnable { endOfQueue() }
                retryRun = r
                main.postDelayed(r, 20_000L)
            }
        }
    }

    /** Keep at most this many songs behind the one playing, so a long night does not grow without end. */
    private fun trimHistory() {
        val keepBehind = 12
        val drop = player.currentMediaItemIndex - keepBehind
        if (drop > 0) player.removeMediaItems(0, drop)
    }

    private fun onError(error: PlaybackException) {
        // Where the song had got to. Losing signal at 2:40 used to start the song again at 0:00
        // because nobody wrote this down.
        val wasAt = player.currentPosition.coerceAtLeast(0L)
        if (!retried) {
            retried = true
            val song = Radio.song
            io.execute {
                Api.resolve(true)   // the address may have moved (wifi to mobile data, or back)
                if (song != null) {
                    val item = item(song)
                    main.post {
                        val at = player.currentMediaItemIndex
                        if (at in 0 until player.mediaItemCount) player.replaceMediaItem(at, item)
                        else player.setMediaItem(item)
                        player.prepare()
                        if (wasAt > 3_000L) player.seekTo(wasAt)    // carry on, do not start again
                        player.play()
                    }
                }
            }
            return
        }
        Radio.status = "That song would not play, moving on"
        Radio.changed()
        ErrorLog.note("player", "\"${Radio.song?.title ?: "a song"}\" would not play: ${error.errorCodeName}", error)
        io.execute { Api.log("android-play-error", Radio.song?.id) }
        endOfQueue()
    }

    /**
     * Ask the server to make one for this channel. Best effort and rate limited to once a minute
     * per channel: the point is to nudge a dry channel, not to pile up a queue from a retry loop.
     */
    private var lastAsk = HashMap<String, Long>()

    private fun askForOne(stationId: String?) {
        val id = stationId ?: return
        if (id == "all" || id == "favorites") return
        val t = android.os.SystemClock.elapsedRealtime()
        if (t - (lastAsk[id] ?: 0L) < 60_000L) return
        lastAsk[id] = t
        io.execute {
            try {
                Api.generate(id)
            } catch (e: Exception) {
                // nothing to do about it out here; the retry will come back round
            }
        }
    }

    private fun ensureBase() {
        if (Api.base == null && Api.resolve(true) == null) throw Api.Unreachable()
    }

    private fun fail(e: Exception) {
        Radio.status = when (e) {
            is Api.AuthNeeded -> "Sign in: open settings with the gear"
            is Api.Unreachable -> "Cannot reach Ten Forward — check the addresses in settings"
            else -> e.message ?: "Something went wrong"
        }
        if (e !is Api.AuthNeeded) ErrorLog.note("radio", Radio.status, e)
        Radio.changed()
    }
}
