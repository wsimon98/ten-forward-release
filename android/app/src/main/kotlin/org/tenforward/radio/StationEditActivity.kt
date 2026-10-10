package org.tenforward.radio

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * A whole channel on one screen: hold a channel in the list to get here, or use + for a new one.
 * Only an admin may save — the server says no to anybody else, and this screen says so plainly.
 */
class StationEditActivity : Activity() {

    companion object {
        const val EXTRA_STATION = "station"
        private val COLORS = listOf("orange", "peach", "gold", "lav", "blue", "teal", "green", "salmon", "pink", "red")
    }

    private var station: Station? = null
    /** What quick create wrote, whole: the form has boxes for most of it, and the rest (its sound set, its
     *  colour, whether swearing is allowed) rides along to the save so nothing quietly falls off. */
    private var draft: JSONObject? = null
    private var draftName: String = ""
    private var lastSaid: String = ""          // what was typed last time, so Try again does not mean type again
    private var working: AlertDialog? = null
    private var favorites = false   // a channel of hearted songs: nothing about it is set here
    private var color: String = "teal"
    private var themeCatalog: List<String> = emptyList()

    private lateinit var head: TextView
    private lateinit var fName: EditText
    private lateinit var fDesc: EditText
    private lateinit var fThemes: EditText
    private lateinit var fStyles: EditText
    private lateinit var fMood: EditText
    private lateinit var fLength: EditText
    private lateinit var fAhead: EditText
    private lateinit var fMale: EditText
    private lateinit var fCover: EditText
    private lateinit var swAuto: Switch
    private lateinit var swInst: Switch
    private lateinit var swOn: Switch
    private lateinit var btnColor: TextView
    private lateinit var out: TextView

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(this)
        ErrorLog.init(this)
        setContentView(R.layout.activity_station_edit)
        Screen.full(this)

        head = findViewById(R.id.head)
        fName = findViewById(R.id.f_name)
        fDesc = findViewById(R.id.f_desc)
        fThemes = findViewById(R.id.f_themes)
        fStyles = findViewById(R.id.f_styles)
        fMood = findViewById(R.id.f_mood)
        fLength = findViewById(R.id.f_length)
        fAhead = findViewById(R.id.f_ahead)
        fMale = findViewById(R.id.f_male)
        fCover = findViewById(R.id.f_cover)
        swAuto = findViewById(R.id.sw_auto)
        swInst = findViewById(R.id.sw_inst)
        swOn = findViewById(R.id.sw_on)
        btnColor = findViewById(R.id.btn_color)
        out = findViewById(R.id.out)

        val raw = intent.getStringExtra(EXTRA_STATION)
        station = if (raw.isNullOrEmpty()) null else try {
            Station.from(JSONObject(raw))
        } catch (e: Exception) {
            null
        }
        favorites = station?.let { it.str("kind") == "favorites" || it.id == "favorites" } ?: false
        fill()

        btnColor.setOnClickListener { pickColor() }
        findViewById<View>(R.id.btn_quick).setOnClickListener { quickCreate() }
        findViewById<View>(R.id.btn_themes).setOnClickListener { pickThemes() }
        findViewById<View>(R.id.btn_save).setOnClickListener { save() }
        findViewById<View>(R.id.btn_make).setOnClickListener { makeOne() }
        findViewById<View>(R.id.btn_delete).setOnClickListener { askDelete() }
        findViewById<View>(R.id.btn_close).setOnClickListener { finish() }

        io.execute {
            val names = try {
                Api.themeNames()
            } catch (e: Exception) {
                emptyList()
            }
            main.post { themeCatalog = names }
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        Screen.keep(this, hasFocus)
    }

    // ---------------------------------------------------------------- what is on the screen
    private fun fill() {
        val st = station
        head.text = if (st == null) "NEW CHANNEL" else st.name.uppercase()
        fName.setText(st?.name ?: "")
        fDesc.setText(st?.description ?: "")
        fThemes.setText(st?.list("themes", ", ") ?: "")
        fStyles.setText(st?.list("style_prompts", "\n") ?: "")
        fMood.setText(st?.str("mood") ?: "")
        fLength.setText((st?.int("duration_s", 300) ?: 300).toString())
        fAhead.setText((st?.int("keep_ahead", 4) ?: 4).toString())
        val male = st?.raw?.opt("male_ratio")
        fMale.setText(if (male == null || male === JSONObject.NULL) "" else Math.round((male.toString().toDoubleOrNull() ?: 0.0) * 100).toString())
        // how often this channel sings a real song instead of writing one; blank follows the setting on the web page
        val cover = st?.raw?.opt("cover_chance")
        fCover.setText(if (cover == null || cover === JSONObject.NULL) "" else Math.round((cover.toString().toDoubleOrNull() ?: 0.0) * 100).toString())
        swAuto.isChecked = st?.on("auto_generate", true) ?: true
        swInst.isChecked = st?.on("instrumental", false) ?: false
        swOn.isChecked = st?.on("enabled", true) ?: true
        color = st?.color?.ifEmpty { "teal" } ?: "teal"
        showColor()
        findViewById<View>(R.id.btn_quick).visibility = if (st == null && !favorites) View.VISIBLE else View.GONE
        findViewById<View>(R.id.btn_delete).visibility = if (st == null || favorites) View.GONE else View.VISIBLE
        findViewById<View>(R.id.btn_make).visibility = if (st == null || favorites) View.GONE else View.VISIBLE
        // a Favorites channel writes nothing, so everything about writing goes away
        for (id in intArrayOf(R.id.row_themes, R.id.f_themes, R.id.lbl_styles, R.id.f_styles, R.id.lbl_mood,
                              R.id.f_mood, R.id.row_numbers, R.id.sw_auto, R.id.sw_inst)) {
            findViewById<View>(id).visibility = if (favorites) View.GONE else View.VISIBLE
        }
        findViewById<TextView>(R.id.counts).text = when {
            favorites -> "${st?.ready ?: 0} songs with a heart on them. Heart one anywhere and it turns up here; take the heart off and it leaves. It keeps playing on its own channel either way."
            st != null -> "${st.unplayed} new · ${st.ready} in all" + if (st.queued > 0) " · ${st.queued} being made" else ""
            else -> "A new channel starts empty and fills itself once it is on."
        }
    }

    private fun showColor() {
        btnColor.text = color.uppercase()
        btnColor.setTextColor(Lcars.color(this, color))
    }

    private fun pickColor() {
        val names = COLORS.map { it.uppercase() }.toTypedArray()
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Colour")
            .setItems(names) { _, which ->
                color = COLORS[which]
                showColor()
            }
            .show()
    }

    private fun pickThemes() {
        if (themeCatalog.isEmpty()) {
            Toast.makeText(this, "The theme list has not arrived yet", Toast.LENGTH_SHORT).show()
            return
        }
        val all = themeCatalog.toTypedArray()
        val chosen = fThemes.text.toString().split(",").map { it.trim().lowercase() }.filter { it.isNotEmpty() }.toMutableSet()
        val checked = BooleanArray(all.size) { chosen.contains(all[it].lowercase()) }
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("What this channel sings about")
            .setMultiChoiceItems(all, checked) { _, which, isChecked ->
                if (isChecked) chosen.add(all[which].lowercase()) else chosen.remove(all[which].lowercase())
            }
            .setPositiveButton("Use these") { _, _ ->
                val keep = all.filter { chosen.contains(it.lowercase()) }
                fThemes.setText(keep.joinToString(", "))
            }
            .setNeutralButton("Anything") { _, _ -> fThemes.setText("") }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ---------------------------------------------------------------- quick create
    private fun quickCreate() {
        val view = layoutInflater.inflate(R.layout.dialog_quick, null)
        val text = view.findViewById<EditText>(R.id.q_text)
        if (lastSaid.isNotEmpty()) text.setText(lastSaid)
        val inst = view.findViewById<Switch>(R.id.q_inst)
        val explicit = view.findViewById<Switch>(R.id.q_explicit)
        val cover = view.findViewById<EditText>(R.id.q_cover)
        val mins = view.findViewById<EditText>(R.id.q_mins)
        val coverRow = view.findViewById<LinearLayout>(R.id.q_cover_row)
        inst.setOnCheckedChangeListener { _, on -> coverRow.visibility = if (on) View.INVISIBLE else View.VISIBLE }
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Quick create")
            .setView(view)
            .setPositiveButton("Build it") { _, _ ->
                val said = text.text.toString().trim()
                if (said.length < 8) {
                    Toast.makeText(this, "Say a sentence or two about it first", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                lastSaid = said
                val body = JSONObject()
                body.put("text", said)
                body.put("instrumental", if (inst.isChecked) 1 else 0)
                body.put("explicit", if (explicit.isChecked) 1 else 0)
                val pct = if (inst.isChecked) 0.0 else (cover.text.toString().trim().toDoubleOrNull() ?: 0.0)
                body.put("cover_chance", Math.max(0.0, Math.min(100.0, pct)) / 100.0)
                mins.text.toString().trim().toDoubleOrNull()?.let { if (it > 0) body.put("minutes", it) }
                build(body)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun build(body: JSONObject) {
        val view = layoutInflater.inflate(R.layout.dialog_working, null)
        val sub = view.findViewById<TextView>(R.id.w_sub)
        val dialog = AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setView(view)
            .setCancelable(false)
            .create()
        dialog.show()
        working = dialog
        out.text = ""
        val started = System.currentTimeMillis()
        val tick = object : Runnable {
            override fun run() {
                val s = (System.currentTimeMillis() - started) / 1000
                sub.text = if (s > 45) "Still going (${s}s). It waits its turn behind a song being planned."
                           else "Writing the channel. Half a minute or so. (${s}s)"
                if (working != null) main.postDelayed(this, 1000)
            }
        }
        main.post(tick)
        io.execute {
            try {
                ensureBase()
                val answer = Api.quickStation(body)
                main.post { stopWorking(); useDraft(answer) }
            } catch (e: Exception) {
                main.post { stopWorking(); out.text = why(e) }
            }
        }
    }

    private fun stopWorking() {
        working?.let { if (it.isShowing) it.dismiss() }
        working = null
    }

    override fun onDestroy() {
        stopWorking()
        super.onDestroy()
    }

    /** What came back goes into the boxes, so it can be read and changed before it is saved. */
    private fun useDraft(answer: JSONObject) {
        val st = answer.optJSONObject("station") ?: return
        draft = st
        draftName = st.optString("name")
        head.text = "NEW CHANNEL"
        fName.setText(st.optString("name"))
        fDesc.setText(st.optString("description"))
        fThemes.setText(joinArray(st.optJSONArray("themes"), ", "))
        fStyles.setText(joinArray(st.optJSONArray("style_prompts"), "\n"))
        fMood.setText(st.optString("mood"))
        fLength.setText(st.optInt("duration_s", 180).toString())
        fAhead.setText(st.optInt("keep_ahead", 2).toString())
        val male = st.opt("male_ratio")
        fMale.setText(if (male == null || male === JSONObject.NULL) "" else Math.round((male.toString().toDoubleOrNull() ?: 0.0) * 100).toString())
        val cover = st.opt("cover_chance")
        fCover.setText(if (cover == null || cover === JSONObject.NULL) "" else Math.round((cover.toString().toDoubleOrNull() ?: 0.0) * 100).toString())
        swInst.isChecked = st.optInt("instrumental", 0) != 0
        swAuto.isChecked = st.optInt("auto_generate", 1) != 0
        swOn.isChecked = st.optInt("enabled", 1) != 0
        color = st.optString("color").ifEmpty { "teal" }
        showColor()
        val notes = StringBuilder()
        val list = answer.optJSONArray("notes")
        if (list != null) for (i in 0 until list.length()) notes.append(list.optString(i)).append(" ")
        notes.append("Nothing is saved until you press SAVE.")
        out.text = notes.toString()
        findViewById<TextView>(R.id.counts).text =
            "Filled in below and NOT SAVED yet. Read it, change anything, then press SAVE to put it on the dial."
        findViewById<ScrollView>(R.id.scroller).smoothScrollTo(0, 0)
        Toast.makeText(this, "Channel written — press SAVE to keep it", Toast.LENGTH_LONG).show()
    }

    private fun joinArray(a: org.json.JSONArray?, separator: String): String {
        if (a == null) return ""
        val parts = ArrayList<String>(a.length())
        for (i in 0 until a.length()) a.optString(i).takeIf { it.isNotEmpty() }?.let { parts.add(it) }
        return parts.joinToString(separator)
    }

    // ---------------------------------------------------------------- saving
    private fun body(): JSONObject {
        // a drafted channel starts from everything quick create wrote (sound set, colour, swearing, replay), and
        // the boxes on this screen are laid over the top of it
        val j = draft?.let { JSONObject(it.toString()) } ?: JSONObject()
        station?.let { j.put("id", it.id) }
        if (draft != null && fName.text.toString().trim() != draftName) j.remove("id")  // a renamed channel gets its own folder
        j.put("name", fName.text.toString().trim())
        j.put("description", fDesc.text.toString().trim())
        j.put("color", color)
        if (favorites) {
            j.put("enabled", if (swOn.isChecked) 1 else 0)
            return j
        }
        j.put("themes", fThemes.text.toString().trim())
        j.put("style_prompts", fStyles.text.toString().trim())
        j.put("mood", fMood.text.toString().trim())
        j.put("duration_s", fLength.text.toString().trim().toIntOrNull() ?: 300)
        j.put("keep_ahead", fAhead.text.toString().trim().toIntOrNull() ?: 4)
        j.put("auto_generate", if (swAuto.isChecked) 1 else 0)
        j.put("instrumental", if (swInst.isChecked) 1 else 0)
        j.put("enabled", if (swOn.isChecked) 1 else 0)
        val male = fMale.text.toString().trim()
        j.put("male_ratio", if (male.isEmpty()) "" else ((male.toDoubleOrNull() ?: 50.0) / 100.0))
        val cover = fCover.text.toString().trim()
        j.put("cover_chance", if (cover.isEmpty()) "" else ((cover.toDoubleOrNull() ?: 0.0) / 100.0))
        return j
    }

    private fun save() {
        if (fName.text.toString().trim().isEmpty()) {
            Toast.makeText(this, "A name first", Toast.LENGTH_SHORT).show()
            return
        }
        out.text = "Saving…"
        val payload = body()
        io.execute {
            try {
                ensureBase()
                val saved = Api.saveStation(payload)
                main.post {
                    Toast.makeText(this, "${saved.name} saved", Toast.LENGTH_SHORT).show()
                    finish()
                }
            } catch (e: Exception) {
                main.post { out.text = why(e) }
            }
        }
    }

    private fun makeOne() {
        val st = station ?: return
        out.text = "Asking for one more song…"
        io.execute {
            try {
                ensureBase()
                Api.generate(st.id)
                main.post { out.text = "It is being made now. It will turn up on ${st.name} when it is done." }
            } catch (e: Exception) {
                main.post { out.text = why(e) }
            }
        }
    }

    private fun askDelete() {
        val st = station ?: return
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("Delete ${st.name}?")
            .setMessage("The channel goes. The songs it made stay where they are.")
            .setPositiveButton("Delete") { _, _ -> delete(st) }
            .setNegativeButton("Keep it", null)
            .show()
    }

    private fun delete(st: Station) {
        out.text = "Deleting…"
        io.execute {
            try {
                ensureBase()
                Api.deleteStation(st.id)
                main.post {
                    Toast.makeText(this, "${st.name} is gone", Toast.LENGTH_SHORT).show()
                    finish()
                }
            } catch (e: Exception) {
                main.post { out.text = why(e) }
            }
        }
    }

    private fun ensureBase() {
        if (Api.base == null && Api.resolve(true) == null) throw Api.Unreachable()
    }

    private fun why(e: Exception): String = when {
        e is Api.AuthNeeded -> "Sign in first: the gear on the first screen."
        e is Api.Unreachable -> "Cannot reach Ten Forward."
        (e.message ?: "").contains("admin", true) -> "Only an admin can change channels. Sign in as one under the gear."
        else -> e.message ?: "That did not work"
    }
}
