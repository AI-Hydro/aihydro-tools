import logging
from pathlib import Path
from ai_hydro.session.store import HydroSession, SESSIONS_DIR

log = logging.getLogger("ai_hydro.session")

def merge_session_shards(session_id: str, shard_ids: list[str] | None = None) -> dict:
    """
    Merge all sub-agent shards into the main session file.
    
    Merge rules:
    - Notes: appended (union)
    - Citations: union
    - Slots: last-writer-wins based on shard file modification time.
    - Shard files are DELETED after successful merge.
    
    Parameters
    ----------
    session_id : str
        The main research session identifier.
    shard_ids : list[str], optional
        Specific shard IDs to merge. If omitted, all shards matching 
        <session_id>.*.shard.json are merged.
        
    Returns
    -------
    dict with shards_merged (int), slots_updated (list), and conflicts (list).
    """
    main_session = HydroSession.load(session_id)
    
    # 1. Discover shards
    if shard_ids:
        shard_paths = [HydroSession._path(session_id, sid) for sid in shard_ids]
        # Filter for existing ones
        shard_paths = [p for p in shard_paths if p.exists()]
    else:
        shard_paths = list(SESSIONS_DIR.glob(f"{session_id}.*.shard.json"))
        
    if not shard_paths:
        return {
            "shards_merged": 0,
            "slots_updated": [],
            "conflicts": [],
            "session_id": session_id
        }
    
    # Sort by modification time so later shards overwrite earlier ones (deterministic LWW)
    shard_paths.sort(key=lambda p: p.stat().st_mtime)
    
    slots_updated = set()
    conflicts = []
    seen_slots = {} # slot -> last_shard_id
    
    for path in shard_paths:
        # Filename is session_id.shard_id.shard.json
        parts = path.name.split('.')
        if len(parts) < 4:
            continue
        shard_id = parts[-3]
        
        try:
            shard = HydroSession.load(session_id, shard_id=shard_id)
            
            # Merge notes
            for note in shard.notes:
                if note not in main_session.notes:
                    main_session.notes.append(note)
            
            # Merge citations
            main_session.add_citations(list(shard.get_citations()))
            
            # Merge interpretation
            if shard.interpretation and not main_session.interpretation:
                main_session.interpretation = shard.interpretation
                
            # Merge slots
            for slot in shard.computed():
                if slot in seen_slots:
                    conflicts.append(
                        f"Slot '{slot}' modified by both shard '{seen_slots[slot]}' "
                        f"and shard '{shard_id}'. Shard '{shard_id}' wins (LWW)."
                    )
                main_session.set(slot, shard.get(slot))
                slots_updated.add(slot)
                seen_slots[slot] = shard_id
                
        except Exception as e:
            log.error(f"Failed to process shard {path}: {e}")
            continue

    # 2. Save merged session (updates updated_at and writes research.md)
    main_session.save()
    
    # 3. Cleanup shards
    for path in shard_paths:
        try:
            path.unlink()
        except Exception as e:
            log.warning(f"Could not delete shard file {path}: {e}")
            
    return {
        "shards_merged": len(shard_paths),
        "slots_updated": sorted(list(slots_updated)),
        "conflicts": conflicts,
        "session_id": session_id,
        "updated_at": main_session.updated_at
    }
