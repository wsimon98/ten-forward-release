package org.tenforward.radio

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

/** Tap a channel to tune to it; hold one to open its editor; SONGS to pick one off it by hand. */
class StationAdapter(
    private val onTap: (Station) -> Unit,
    private val onHold: (Station) -> Unit,
    private val onSongs: (Station) -> Unit,
) : RecyclerView.Adapter<StationAdapter.Row>() {

    private val items = ArrayList<Station>()
    var onAir: String? = null

    fun submit(list: List<Station>) {
        items.clear()
        items.addAll(list)
        notifyDataSetChanged()
    }

    class Row(v: View) : RecyclerView.ViewHolder(v) {
        val bar: View = v.findViewById(R.id.bar)
        val name: TextView = v.findViewById(R.id.name)
        val desc: TextView = v.findViewById(R.id.desc)
        val stats: TextView = v.findViewById(R.id.stats)
        val onair: TextView = v.findViewById(R.id.onair)
        val songs: TextView = v.findViewById(R.id.btn_songs)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Row =
        Row(LayoutInflater.from(parent.context).inflate(R.layout.item_station, parent, false))

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: Row, position: Int) {
        val st = items[position]
        val ctx = holder.itemView.context
        holder.bar.setBackgroundColor(Lcars.color(ctx, st.color))
        holder.name.text = st.name
        holder.name.setTextColor(Lcars.color(ctx, st.color))
        holder.desc.text = st.description
        holder.desc.visibility = if (st.description.isEmpty()) View.GONE else View.VISIBLE
        val bits = ArrayList<String>()
        if (st.str("kind") == "favorites" || st.id == "favorites") {
            bits.add("${st.ready} hearted")
        } else {
            bits.add("${st.unplayed} new")
            bits.add("${st.ready} in all")
            if (st.queued > 0) bits.add("${st.queued} being made")
            if (st.instrumental) bits.add("instrumental")
        }
        holder.stats.text = bits.joinToString(" · ")
        holder.onair.visibility = if (st.id == onAir) View.VISIBLE else View.INVISIBLE
        holder.songs.setTextColor(Lcars.color(ctx, st.color))
        holder.songs.setOnClickListener { onSongs(st) }
        holder.itemView.setOnClickListener { onTap(st) }
        holder.itemView.setOnLongClickListener {
            onHold(st)
            true
        }
    }
}
