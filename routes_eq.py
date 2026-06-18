#!/usr/bin/env python3
"""Vinyl AirPlay: volume + EQ routes (bass/treble shelves, 5-band parametric, presets).

Thin HTTP layer over state.eq; the DSP lives in audio_eq. Shares AppState via app_state.
"""

from fastapi import APIRouter

from app_state import state
from config import save_settings

router = APIRouter()

EQ_PRESETS = {
    "flat":        {"bands": [0, 0, 0, 0, 0],       "bass": 0, "treble": 0},
    "jazz":        {"bands": [2, 1, -1, 2, 3],      "bass": 3, "treble": 2},
    "rock":        {"bands": [4, 2, -1, 3, 4],      "bass": 4, "treble": 3},
    "hip_hop":     {"bands": [5, 4, 0, 1, 2],       "bass": 5, "treble": 1},
    "electronic":  {"bands": [5, 2, 0, 2, 4],       "bass": 4, "treble": 4},
    "vocal":       {"bands": [-2, -1, 3, 4, 1],     "bass": -2, "treble": 1},
    "classical":   {"bands": [1, 0, 0, 1, 3],       "bass": 1, "treble": 3},
    "bass_boost":  {"bands": [6, 4, 0, 0, 0],       "bass": 6, "treble": 0},
    "warm":        {"bands": [3, 2, 0, -1, -2],     "bass": 3, "treble": -2},
    "bright":      {"bands": [-1, 0, 1, 3, 5],      "bass": -1, "treble": 5},
}


@router.post("/api/volume")
async def set_volume(body: dict):
    volume = int(body.get("volume", 80))
    state.eq.set_volume(volume)
    state.settings["volume"] = volume
    save_settings(state.settings)
    return {"ok": True, "volume": volume}


@router.post("/api/eq")
async def set_eq(body: dict):
    bass   = float(body.get("bass",   state.settings.get("bass",   0)))
    treble = float(body.get("treble", state.settings.get("treble", 0)))
    state.eq.set_eq(bass, treble)
    state.settings["bass"] = bass
    state.settings["treble"] = treble
    save_settings(state.settings)
    return {"ok": True, "bass": bass, "treble": treble}


@router.post("/api/eq/bands")
async def set_eq_bands(body: dict):
    """Set 5-band parametric EQ. body: { bands: [60hz, 250hz, 1khz, 3.5khz, 10khz] }"""
    bands = body.get("bands", [0, 0, 0, 0, 0])
    if not isinstance(bands, list) or len(bands) != 5:
        return {"ok": False, "error": "bands must be a list of 5 values"}
    state.eq.set_bands(bands)
    state.settings["eq_bands"] = state.eq.band_values
    save_settings(state.settings)
    return {"ok": True, "bands": state.eq.band_values}


@router.get("/api/eq/bands")
async def get_eq_bands():
    return {"ok": True, "bands": state.eq.band_values}


@router.get("/api/eq/presets")
async def get_eq_presets():
    return {"ok": True, "presets": EQ_PRESETS}


@router.post("/api/eq/preset/{name}")
async def apply_eq_preset(name: str):
    preset = EQ_PRESETS.get(name)
    if not preset:
        return {"ok": False, "error": f"Unknown preset: {name}"}
    state.eq.set_eq(preset["bass"], preset["treble"])
    state.eq.set_bands(preset["bands"])
    state.settings["bass"] = preset["bass"]
    state.settings["treble"] = preset["treble"]
    state.settings["eq_bands"] = preset["bands"]
    state.settings["eq_preset"] = name
    save_settings(state.settings)
    return {"ok": True, "preset": name, "bass": preset["bass"],
            "treble": preset["treble"], "bands": preset["bands"]}
