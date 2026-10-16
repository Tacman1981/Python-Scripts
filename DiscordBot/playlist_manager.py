import os
import json
from typing import List, Dict, Any, Optional

PLAYLIST_FILE = "playlist.json"


class PlaylistStorage:
    """Manages persistent server playlist stored in a local JSON file."""
    def __init__(self, filepath: str = PLAYLIST_FILE):
        self.filepath = filepath
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    def load_tracks(self) -> List[Dict[str, Any]]:
        self._ensure_file()
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading playlist: {e}")
            return []

    def save_tracks(self, tracks: List[Dict[str, Any]]):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(tracks, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving playlist: {e}")

    def add_track(self, title: str, webpage_url: str, duration: Optional[int], thumbnail: Optional[str], added_by: str) -> int:
        tracks = self.load_tracks()
        tracks.append({
            "title": title,
            "webpage_url": webpage_url,
            "duration": duration,
            "thumbnail": thumbnail,
            "added_by": added_by
        })
        self.save_tracks(tracks)
        return len(tracks)

    def add_tracks_bulk(self, new_tracks: List[Dict[str, Any]]) -> int:
        tracks = self.load_tracks()
        tracks.extend(new_tracks)
        self.save_tracks(tracks)
        return len(tracks)

    def remove_track(self, index: int) -> Optional[Dict[str, Any]]:
        """1-based index removal."""
        tracks = self.load_tracks()
        if 1 <= index <= len(tracks):
            removed = tracks.pop(index - 1)
            self.save_tracks(tracks)
            return removed
        return None

    def clear(self):
        self.save_tracks([])
