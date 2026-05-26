#!/usr/bin/env python3
"""
FORGE Analytics — .rofl Replay Server with Riot API enrichment
Run:  python3 server.py
Open: http://localhost:7890
"""
import json, struct, os, re, traceback, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
RIOT_API_KEY = "RGAPI-47d9757f-fc21-45b6-aac9-0e95807493a7"

# Auto-detected per-file; override here if needed (na1, kr, euw1, eun1, jp1, br1, oc1, tr1, ru)
DEFAULT_PLATFORM = "na1"
PLATFORM_TO_REGION = {
    "na1":"americas","br1":"americas","la1":"americas","la2":"americas",
    "kr":"asia","jp1":"asia",
    "euw1":"europe","eun1":"europe","tr1":"europe","ru":"europe",
    "oc1":"sea",
}

_cache = {}

# ─────────────────────────────────────────────
# RIOT API HELPERS
# ─────────────────────────────────────────────
def riot_get(url, retries=3):
    if url in _cache:
        return _cache[url]
    headers = {"X-Riot-Token": RIOT_API_KEY, "Accept": "application/json", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
                _cache[url] = data
                return data
        except HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"  HTTP {e.code} for {url}: {body[:200]}")
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                print(f"  Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after); continue
            elif e.code == 404:
                return None
            else:
                raise
        except URLError as e:
            if attempt == retries - 1: raise
            time.sleep(1)
    return None


def platform_from_filename(filename: str) -> str:
    """Extract platform from filename like NA1-1234567.rofl → na1"""
    m = re.match(r"([A-Za-z0-9]+)-\d+", os.path.basename(filename))
    if m:
        p = m.group(1).lower()
        if p in PLATFORM_TO_REGION:
            return p
    return DEFAULT_PLATFORM


def enrich_via_puuid(base_result, fake_puuid, game_length_ms, platform):
    """
    Dev keys can't look up by summoner ID or the fake rofl PUUID.
    Use RIOT_ID_GAME_NAME -> /account/v1/accounts/by-riot-id/{name}/{tag}
    to get the real PUUID, then look up matches.
    """
    region = PLATFORM_TO_REGION.get(platform, "americas")

    # Find a player with a riot game name from the players list
    riot_game_name = ""
    riot_tag = ""
    for p in base_result.get("players", []):
        name = p.get("summonerName", "")
        if name and not name.startswith("Player"):
            riot_game_name = name
            # tag defaults to platform tag e.g. NA1
            riot_tag = platform.upper().rstrip("1") or "NA1"
            break

    real_puuid = ""

    if riot_game_name:
        # Try common tags: NA1, NA, 1, platform name
        tags_to_try = [platform.upper(), "NA1", "NA", "1", platform.upper().rstrip("1")]
        # Remove dupes while preserving order
        seen = set()
        tags_to_try = [t for t in tags_to_try if t and not (t in seen or seen.add(t))]

        for tag in tags_to_try:
            url = f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{riot_game_name}/{tag}"
            print(f"  Trying account lookup: {riot_game_name}#{tag}")
            account_data = riot_get(url)
            if account_data and account_data.get("puuid"):
                real_puuid = account_data["puuid"]
                riot_tag = tag
                print(f"  Found account: {riot_game_name}#{tag} -> PUUID {real_puuid[:30]}...")
                break

    if not real_puuid:
        base_result["riotApiError"] = (
            f"Could not resolve account for '{riot_game_name}'. "
            "Tip: try naming your .rofl file as 'Name#TAG_vs_opponent.rofl' "
            "so the tag is explicit."
        )
        print(f"  {base_result['riotApiError']}")
        return base_result

    # Build a set of known player names from the rofl — need 3+ matches to be confident
    known_names = {
        p.get("summonerName","").lower()
        for p in base_result.get("players", [])
        if p.get("summonerName","") and not p.get("summonerName","").startswith("Player")
    }
    target_secs = game_length_ms // 1000
    print(f"  Known players from rofl: {known_names}")
    print(f"  Target duration: {target_secs}s")

    # Search in batches of 20 until we find a match with 3+ name overlaps
    # or exhaust 100 matches
    for start in range(0, 100, 20):
        print(f"  Fetching matches {start}-{start+20} for {riot_game_name}#{riot_tag}...")
        match_ids = riot_get(
            f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{real_puuid}/ids?start={start}&count=20"
        )
        if not match_ids:
            break

        for match_id in match_ids:
            mdata = riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}")
            if not mdata: continue
            info = mdata.get("info", {})
            duration = info.get("gameDuration", 0)
            dur_delta = abs(duration - target_secs)

            api_names = {
                (p.get("riotIdGameName") or p.get("summonerName","")).lower()
                for p in info.get("participants", [])
            }
            name_overlap = len(known_names & api_names)
            print(f"  {match_id}: dur={duration}s Δ={dur_delta}s names={name_overlap}/{len(known_names)} {api_names & known_names}")

            # Confident match: duration within 120s AND 1+ name overlap
            if name_overlap >= 1 and dur_delta <= 120:
                print(f"  ✓ Confirmed match: {match_id} ({name_overlap} players verified)")
                return enrich_with_riot_api(base_result, mdata, region)

            # Possible match: duration within 30s AND 1-2 names match — keep as candidate
            # but keep searching for something better

    base_result["riotApiError"] = (
        f"Could not find your game among the last 100 matches for {riot_game_name}. "
        f"Known players: {known_names}. Target: {target_secs}s."
    )
    print(f"  {base_result['riotApiError']}")
    return base_result


def enrich_with_riot_api(base_result, match_data, region):
    match_id = match_data["metadata"]["matchId"]
    info = match_data["info"]
    print(f"  Enriching with {match_id}...")

    # Timeline for build paths, skill order, objectives
    timeline = riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline")

    participants = info.get("participants", [])
    build_paths   = {i: [] for i in range(len(participants))}
    skill_orders  = {i: [] for i in range(len(participants))}
    spell_casts   = {i: {"spell1": 0, "spell2": 0} for i in range(len(participants))}
    ability_casts = {i: {"Q": 0, "W": 0, "E": 0, "R": 0} for i in range(len(participants))}

    if timeline:
        for frame in timeline.get("info", {}).get("frames", []):
            ts_ms = frame.get("timestamp", 0)
            ts_min = round(ts_ms / 60000, 2)
            for event in frame.get("events", []):
                etype = event.get("type", "")
                pid = event.get("participantId", 0) - 1  # 0-indexed

                if etype == "ITEM_PURCHASED" and 0 <= pid < len(participants):
                    build_paths[pid].append({
                        "itemId": event["itemId"],
                        "timestamp": ts_ms,
                        "minute": round(ts_min, 1),
                    })

                elif etype == "SKILL_LEVEL_UP" and 0 <= pid < len(participants):
                    slot_map = {1: "Q", 2: "W", 3: "E", 4: "R"}
                    skill = slot_map.get(event.get("skillSlot", 0), "?")
                    skill_orders[pid].append({
                        "skill": skill,
                        "level": len(skill_orders[pid]) + 1,
                        "timestamp": ts_ms,
                        "minute": round(ts_min, 1),
                    })

    # Build enriched players
    enriched = []
    for i, p in enumerate(participants):
        perks = p.get("perks", {})
        primary_runes = {}
        secondary_runes = {}
        for style in perks.get("styles", []):
            selections = [{"perkId": s["perk"], "var1": s.get("var1", 0), "var2": s.get("var2", 0), "var3": s.get("var3", 0)}
                          for s in style.get("selections", [])]
            if style.get("description") == "primaryStyle":
                primary_runes = {"styleId": style["style"], "perks": selections}
            elif style.get("description") == "subStyle":
                secondary_runes = {"styleId": style["style"], "perks": selections}

        enriched.append({
            "summonerName":   p.get("riotIdGameName") or p.get("summonerName", f"Player{i+1}"),
            "riotId":         f"{p.get('riotIdGameName','')}#{p.get('riotIdTagline','')}",
            "puuid":          p.get("puuid", ""),
            "championName":   p.get("championName", ""),
            "championId":     p.get("championId", 0),
            "team":           p.get("teamId", 100 if i < 5 else 200),
            "position":       (p.get("teamPosition") or p.get("individualPosition") or ["TOP","JUNGLE","MID","BOTTOM","SUPPORT"][i%5]).replace("UTILITY","SUPPORT"),
            "win":            p.get("win", False),
            "kills":          p.get("kills", 0),
            "deaths":         p.get("deaths", 0),
            "assists":        p.get("assists", 0),
            "cs":             p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "gold":           p.get("goldEarned", 0),
            "goldSpent":      p.get("goldSpent", 0),
            "damage":         p.get("totalDamageDealtToChampions", 0),
            "damageTaken":    p.get("totalDamageTaken", 0),
            "healingDone":    p.get("totalHeal", 0),
            "shieldingDone":  p.get("totalDamageShieldedOnTeammates", 0),
            "visionScore":    p.get("visionScore", 0),
            "wardsPlaced":    p.get("wardsPlaced", 0),
            "wardsKilled":    p.get("wardsKilled", 0),
            "controlWards":   p.get("visionWardsBoughtInGame", 0),
            "level":          p.get("champLevel", 18),
            "pentaKills":     p.get("pentaKills", 0),
            "quadraKills":    p.get("quadraKills", 0),
            "tripleKills":    p.get("tripleKills", 0),
            "doubleKills":    p.get("doubleKills", 0),
            "firstBlood":     p.get("firstBloodKill", False),
            "turretKills":    p.get("turretKills", 0),
            "objectivesStolen": p.get("objectivesStolen", 0),
            "items":          [p.get(f"item{n}", 0) for n in range(7)],
            "spell1Id":       p.get("summoner1Id", 0),
            "spell2Id":       p.get("summoner2Id", 0),
            "spell1Casts":    p.get("summoner1Casts", 0),
            "spell2Casts":    p.get("summoner2Casts", 0),
            "spell1Name":     summoner_name(p.get("summoner1Id", 0)),
            "spell2Name":     summoner_name(p.get("summoner2Id", 0)),
            "qCasts":         p.get("spell1Casts", 0),
            "wCasts":         p.get("spell2Casts", 0),
            "eCasts":         p.get("spell3Casts", 0),
            "rCasts":         p.get("spell4Casts", 0),
            "primaryRunes":   primary_runes,
            "secondaryRunes": secondary_runes,
            "statPerks":      perks.get("statPerks", {}),
            "buildPath":      build_paths.get(i, []),
            "skillOrder":     skill_orders.get(i, []),
        })

    t1_players = [p for p in enriched if p["team"] == 100]
    t2_players = [p for p in enriched if p["team"] == 200]
    teams_info = {t["teamId"]: t for t in info.get("teams", [])}
    t1_info = teams_info.get(100, {})
    t2_info = teams_info.get(200, {})

    def objectives(ti):
        obj = ti.get("objectives", {})
        return {k: {"kills": obj.get(k,{}).get("kills",0), "first": obj.get(k,{}).get("first",False)}
                for k in ["baron","dragon","tower","inhibitor","riftHerald","horde","champion"]}

    t1_name = base_result.get("team1", {}).get("name", "Blue")
    t2_name = base_result.get("team2", {}).get("name", "Red")

    def build_team(name, tid, players, tinfo):
        return {
            "name": name, "tag": name[:3].upper(), "id": tid,
            "players": players,
            "win": tinfo.get("win", False),
            "totalKills": sum(p["kills"] for p in players),
            "totalGold": sum(p["gold"] for p in players),
            "totalDamage": sum(p["damage"] for p in players),
            "bans": [b.get("championId", 0) for b in tinfo.get("bans", [])],
            "objectives": objectives(tinfo),
        }

    # Dragon timeline
    dragon_kills = []
    if timeline:
        for frame in timeline.get("info", {}).get("frames", []):
            for event in frame.get("events", []):
                if event.get("type") == "ELITE_MONSTER_KILL" and event.get("monsterType") == "DRAGON":
                    dragon_kills.append({
                        "type": event.get("monsterSubType", "UNKNOWN_DRAGON").replace("_DRAGON",""),
                        "team": event.get("killerTeamId", 0),
                        "minute": round(event.get("timestamp",0)/60000, 1),
                    })

    base_result.update({
        "riotMatchId":  match_id,
        "riotEnriched": True,
        "gameDuration": info.get("gameDuration", base_result.get("gameDuration", 0)),
        "gameVersion":  info.get("gameVersion", base_result.get("gameVersion", "")),
        "gameMode":     info.get("gameMode", "CLASSIC"),
        "players":      enriched,
        "team1":        build_team(t1_name, 100, t1_players, t1_info),
        "team2":        build_team(t2_name, 200, t2_players, t2_info),
        "winner":       100 if t1_info.get("win") else 200,
        "dragonKills":  dragon_kills,
    })
    print(f"  Done: {len(enriched)} players, {sum(len(v) for v in build_paths.values())} item events, {sum(len(v) for v in skill_orders.values())} skill events")
    return base_result


SUMMONER_SPELL_MAP = {4:"Flash",14:"Ignite",12:"Teleport",11:"Smite",21:"Barrier",
    3:"Exhaust",7:"Heal",6:"Ghost",1:"Cleanse",13:"Clarity",32:"Mark"}

def summoner_name(sid):
    return SUMMONER_SPELL_MAP.get(sid, f"Spell{sid}")


# ─────────────────────────────────────────────
# ROFL PARSER
# ─────────────────────────────────────────────
ROFL_MAGIC = b"RIOT:RECORDING\x00"
_last_debug = {}

def parse_rofl(data: bytes, filename: str) -> dict:
    errors = []
    meta = {}
    stats_players = []

    if data[:15] != ROFL_MAGIC:
        errors.append(f"Magic mismatch: {data[:15]!r}")

    try:
        meta_offset = struct.unpack_from("<I", data, 277)[0]
        meta_len    = struct.unpack_from("<I", data, 281)[0]
        if 0 < meta_offset and meta_offset + meta_len <= len(data):
            meta = json.loads(data[meta_offset: meta_offset + meta_len].decode("utf-8", errors="replace"))
    except Exception as e:
        errors.append(f"Header: {e}")

    if not meta:
        raw = data.decode("utf-8", errors="replace")
        for marker in ['{"gameLength"', '{"GameLength"', '{"gameVersion"']:
            idx = raw.find(marker)
            if idx < 0: continue
            depth = end = 0
            for i in range(idx, min(idx+500_000, len(raw))):
                if raw[i] == '{': depth += 1
                elif raw[i] == '}':
                    depth -= 1
                    if depth == 0: end = i+1; break
            try: meta = json.loads(raw[idx:end]); break
            except: pass

    stats_raw = meta.get("statsJson") or meta.get("StatsJson")
    if isinstance(stats_raw, str):
        try: stats_players = json.loads(stats_raw)
        except: pass
    elif isinstance(stats_raw, list):
        stats_players = stats_raw

    if not stats_players:
        raw = data.decode("utf-8", errors="replace")
        for marker in ['"statsJson":"[', '"StatsJson":"[']:
            idx = raw.find(marker)
            if idx < 0: continue
            start = raw.index('[', idx)
            depth = end = 0
            for i in range(start, min(start+2_000_000, len(raw))):
                c = raw[i]
                if c in '[{': depth += 1
                elif c in ']}':
                    depth -= 1
                    if depth == 0: end = i+1; break
            try: stats_players = json.loads(raw[start:end]); break
            except: pass

    # Debug info
    _last_debug.update({
        "meta_keys": list(meta.keys()),
        "meta_sample": {k: str(v)[:200] for k,v in meta.items() if "stats" not in k.lower()},
        "raw_game_length": int(meta.get("gameLength") or meta.get("GameLength") or 0),
        "player0_keys": list(stats_players[0].keys())[:30] if stats_players and isinstance(stats_players[0],dict) else [],
        "player0_name_fields": {k:str(v)[:100] for k,v in (stats_players[0] if stats_players and isinstance(stats_players[0],dict) else {}).items()
                                if any(x in k.lower() for x in ["name","summoner","puuid","account","player"])},
    })

    base = transform_meta(meta, stats_players, filename, errors)

    # Riot API enrichment
    if RIOT_API_KEY and not RIOT_API_KEY.startswith("YOUR_"):
        platform = platform_from_filename(filename)
        region = PLATFORM_TO_REGION.get(platform, "americas")
        try:
            # Strategy 1: match ID is directly in the filename e.g. NA1-5568364943.rofl
            match_id_from_file = None
            m = re.match(r"([A-Za-z0-9]+)-([0-9]+)[.]rofl$", os.path.basename(filename), re.IGNORECASE)
            if m:
                match_id_from_file = f"{m.group(1).upper()}_{m.group(2)}"
                print(f"  Match ID from filename: {match_id_from_file}")

            # Strategy 2: gameId in metadata
            game_id = str(meta.get("gameId") or meta.get("GameId") or "")

            if match_id_from_file:
                mdata = riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id_from_file}")
                if mdata:
                    print(f"  Found match directly from filename: {match_id_from_file}")
                    base = enrich_with_riot_api(base, mdata, region)
                else:
                    print(f"  Filename match ID {match_id_from_file} not found, falling back to PUUID search")
                    raise ValueError("fallback")
            elif game_id:
                mdata = riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{platform.upper()}_{game_id}")
                if mdata:
                    base = enrich_with_riot_api(base, mdata, region)
                else:
                    raise ValueError(f"Match {platform.upper()}_{game_id} not found, falling back")
            else:
                raise ValueError("no direct ID, falling back to PUUID search")

        except ValueError:
            # Fallback: search by player name
            try:
                puuid = next((str(p.get("PUUID") or p.get("puuid",""))
                              for p in stats_players if isinstance(p,dict)
                              and (p.get("PUUID") or p.get("puuid",""))), "")
                game_length_ms = int(meta.get("gameLength") or meta.get("GameLength") or 0)
                base = enrich_via_puuid(base, puuid, game_length_ms, platform)
            except Exception as e2:
                print(f"  Fallback also failed: {e2}")
                base["riotApiError"] = str(e2)
        except Exception as e:
            print(f"  Riot API failed: {e}")
            traceback.print_exc()
            base["riotApiError"] = str(e)

    return base


def rfield(p, *keys):
    for k in keys:
        for c in [k, k.upper(), k.lower(), k[0].upper()+k[1:]]:
            if c in p and p[c] not in (None, ""):
                return p[c]
    return 0


def transform_meta(meta, stats, filename, errors):
    POSITIONS = ["TOP","JUNGLE","MID","BOTTOM","SUPPORT"]
    raw_len  = int(rfield(meta,"gameLength","GameLength") or 0)
    game_len = raw_len // 1000 if raw_len > 7200 else raw_len
    game_ver = str(rfield(meta,"gameVersion","GameVersion") or "Unknown")

    print(f"  [PARSE] gameLength raw={raw_len} -> {game_len}s  players_found={len(stats)}")
    if stats and isinstance(stats[0], dict):
        p0 = stats[0]
        print(f"  [PARSE] Player[0] name fields: { {k:str(v)[:50] for k,v in p0.items() if any(x in k.lower() for x in ['name','puuid','summoner'])} }")

    players = []
    for i, p in enumerate(stats):
        if not isinstance(p, dict): continue
        team_id = int(rfield(p,"TEAM","team") or (100 if i<5 else 200))
        win = str(rfield(p,"WIN","win","WINNER") or "0").lower() in ("1","true","win")
        cs  = int(rfield(p,"MINIONS_KILLED","minionsKilled") or 0) + int(rfield(p,"NEUTRAL_MINIONS_KILLED","neutralMinionsKilled") or 0)

        name = (str(rfield(p,"RIOT_ID_GAME_NAME") or "").strip() or
                str(rfield(p,"NAME","SUMMONER_NAME","summonerName") or "").strip() or
                f"Player{i+1}")

        # Champion from Skins keys e.g. 2026_S1A1_Skins_Ashe = "1"
        champ = str(rfield(p,"SKIN","CHAMPION_NAME","championName") or "")
        if not champ or champ.startswith("Champion"):
            for k,v in p.items():
                if "_Skins_" in k and str(v) == "1":
                    champ = k.split("_Skins_")[-1]; break

        pos_num = int(rfield(p,"PLAYER_POSITION") or 0)
        pos = str(rfield(p,"INDIVIDUAL_POSITION","TEAM_POSITION","position") or
                  {1:"TOP",2:"JUNGLE",3:"MID",4:"BOTTOM",5:"SUPPORT"}.get(pos_num, POSITIONS[i%5]))
        pos = pos.replace("UTILITY","SUPPORT")

        puuid = str(rfield(p,"PUUID","puuid") or "")

        players.append({
            "summonerName": name,
            "championName": champ or f"Champion{i+1}",
            "puuid": puuid,
            "team": team_id,
            "position": pos,
            "kills":   int(rfield(p,"CHAMPIONS_KILLED","kills") or 0),
            "deaths":  int(rfield(p,"NUM_DEATHS","deaths") or 0),
            "assists": int(rfield(p,"ASSISTS","assists") or 0),
            "cs": cs,
            "gold":    int(rfield(p,"GOLD_EARNED","goldEarned","gold") or 0),
            "damage":  int(rfield(p,"TOTAL_DAMAGE_DEALT_TO_CHAMPIONS","totalDamageDealtToChampions") or 0),
            "visionScore": int(rfield(p,"VISION_SCORE","visionScore") or 0),
            "wardsPlaced": int(rfield(p,"WARD_PLACED","wardsPlaced") or 0),
            "win": win,
            "items": [int(rfield(p,f"ITEM{n}",f"item{n}") or 0) for n in range(6)],
            "level": int(rfield(p,"LEVEL","level") or 18),
            "buildPath":  [], "skillOrder":    [],
            "primaryRunes": {}, "secondaryRunes": {},
            "spell1Id": int(rfield(p,"SUMMONER_SPELL_1","summoner1Id") or 0),
            "spell2Id": int(rfield(p,"SUMMONER_SPELL_2","summoner2Id") or 0),
            "spell1Name": summoner_name(int(rfield(p,"SUMMONER_SPELL_1","summoner1Id") or 0)),
            "spell2Name": summoner_name(int(rfield(p,"SUMMONER_SPELL_2","summoner2Id") or 0)),
            "_summonerId": str(rfield(p,"SUMMONER_ID","summonerId") or ""),
        })

    t1 = [p for p in players if p["team"]==100]
    t2 = [p for p in players if p["team"]==200]
    winner = next((p["team"] for p in players if p["win"]), 100)

    base = os.path.splitext(filename)[0]
    m = re.search(r"(.+?)[\s_\-]+vs[\s_\-]+(.+)", base, re.IGNORECASE)
    t1_name = m.group(1).strip() if m else str(rfield(meta,"blueTeamTag") or "Blue")
    t2_name = m.group(2).strip() if m else str(rfield(meta,"redTeamTag") or "Red")

    def mkteam(name, tid, plist, win):
        return {"name":name,"tag":name[:3].upper(),"id":tid,"players":plist,
                "totalKills":sum(p["kills"] for p in plist),
                "totalGold":sum(p["gold"] for p in plist),
                "totalDamage":sum(p["damage"] for p in plist),"win":win}

    # Grab first player's summoner ID for API lookup
    first_summoner_id = next((p["_summonerId"] for p in players if p.get("_summonerId")), "")

    return {
        "filename": filename, "gameVersion": game_ver,
        "gameDuration": game_len, "gameDate": "",
        "winner": winner,
        "team1": mkteam(t1_name, 100, t1, winner==100),
        "team2": mkteam(t2_name, 200, t2, winner==200),
        "players": players, "parseErrors": errors, "riotEnriched": False,
        "_summoner_id": first_summoner_id,
    }


# ─────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────
FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "index.html")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = open(FRONTEND_PATH, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_json({"error": "index.html not found"}, 404)
        elif path == "/debug":
            self.send_json(_last_debug)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/parse":
            self.send_json({"error": "Unknown endpoint"}, 404); return
        try:
            ct = self.headers.get("Content-Type", "")
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            results, errors = [], []

            if "multipart/form-data" in ct:
                boundary = next((p.strip()[9:].strip('"') for p in ct.split(";") if p.strip().startswith("boundary=")), None)
                if boundary:
                    for fname, fdata in parse_multipart(raw, boundary.encode()):
                        try: results.append(parse_rofl(fdata, fname))
                        except Exception as e: errors.append({"file": fname, "error": str(e), "trace": traceback.format_exc()})
            elif ct == "application/octet-stream":
                fname = self.headers.get("X-Filename", "replay.rofl")
                try: results.append(parse_rofl(raw, fname))
                except Exception as e: errors.append({"file": fname, "error": str(e)})

            self.send_json({"results": results, "errors": errors})
        except Exception as e:
            self.send_json({"error": str(e), "trace": traceback.format_exc()}, 500)


def parse_multipart(body, boundary):
    files = []
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part: continue
        hdr, _, content = part.partition(b"\r\n\r\n")
        content = content.rstrip(b"\r\n--")
        fname = next((t.strip()[9:].strip('"\'') for t in hdr.decode("utf-8",errors="replace").split(";") if t.strip().startswith("filename=")), "upload.rofl")
        if content: files.append((fname, content))
    return files


if __name__ == "__main__":
    PORT = 7890
    print(f"""
╔══════════════════════════════════════════════════╗
║    FORGE Analytics — Replay Server v3            ║
╠══════════════════════════════════════════════════╣
║  http://localhost:{PORT}                            ║
║  Riot API: configured (auto-detects region)      ║
║  Debug:    http://localhost:{PORT}/debug            ║
╚══════════════════════════════════════════════════╝
""")
    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")