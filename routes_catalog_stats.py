#!/usr/bin/env python3
"""Vinyl AirPlay: read-only catalog analytics routes.

Duplicates detection plus the dashboard stats (heatmap, genre/artist/decade
breakdowns, on-this-day, weekly trend). All queries live in catalog.py.
"""

from fastapi import APIRouter

import catalog as cat

router = APIRouter()


@router.get("/api/catalog/duplicates")
async def get_duplicate_albums():
    """Get groups of potential duplicate albums."""
    groups = cat.find_duplicate_albums(similarity_threshold=0.80)
    return {"duplicate_groups": groups}


@router.get("/api/catalog/heatmap")
async def get_heatmap():
    """Get play activity heatmap for the last 6 months."""
    heatmap = cat.get_play_heatmap(months=6)
    return {"heatmap": heatmap}


@router.get("/api/catalog/genre-stats")
async def get_genre_stats():
    """Get album count per genre."""
    genres = cat.get_genre_stats()
    return {"genres": genres}


@router.get("/api/catalog/artist-stats")
async def get_artist_stats():
    """Get top 10 artists by album count."""
    artists = cat.get_artist_stats(limit=10)
    return {"artists": artists}


@router.get("/api/catalog/decade-stats")
async def get_decade_stats():
    """Get album count by decade."""
    decades = cat.get_decade_stats()
    return {"decades": decades}


@router.get("/api/catalog/on-this-day")
async def get_on_this_day():
    """Get albums played on this date in prior years."""
    albums = cat.get_on_this_day()
    return {"albums": albums}


@router.get("/api/catalog/weekly-trend")
async def get_weekly_trend():
    """Get play count per week for the last 12 weeks."""
    trend = cat.get_weekly_trend(weeks=12)
    return {"weeks": trend}
