// ad-parser — reads a Dota 2 .dem replay file and prints the Ability Draft
// pick sequence in TRUE CHRONOLOGICAL ORDER as a JSON array to stdout.
//
// HOW WE GET CHRONOLOGICAL PICK ORDER
// -----------------------------------
// The draft phase IS in the demo, but the chronological order is encoded
// across MULTIPLE fields, not in a single array.
//
// The signal: CDOTAGamerulesProxy.m_pGameRules.m_bAbilityDraftCurrentPlayerHasPicked
// flips false→true once per pick. Combined with the round number and
// "player tracker" position, this gives us the chronological pick sequence.
//
//   m_nAbilityDraftRoundNumber  — round 0-4 (5 rounds)
//   m_nAbilityDraftPlayerTracker — 0-9, position in this round's pick order
//   m_bAbilityDraftCurrentPlayerHasPicked — false→true on each pick
//
// Round 0 progresses tracker 0→9 (forward); round 1 progresses 9→0 (snake
// reverse); etc. Each tick at which hasPicked transitions false→true is a
// new pick event.
//
// The tracker is the POSITION in the snake draft order, not a player_id.
// Each player has m_unPickOrder on CDOTA_PlayerResource.m_vecPlayerTeamData
// (1-10) which gives their position. We build a tracker→player map.
//
// MODEL vs ABILITY PICK
// ---------------------
// At every pick tick, exactly one of the player's items is committed:
//   - If m_vecPlayerTeamData[player].m_nSelectedHeroID changes from 0 at
//     this tick, the pick is a MODEL → we know the hero ID.
//   - Otherwise, the pick is an ABILITY. We know the player and tick;
//     the specific ability is taken from the player's m_vecAbilities
//     (final state, captured when hero entity spawns post-draft).
//
// Each player makes 5 picks total: 4 abilities + 1 model = 50 picks across
// 10 players. The order within each player's 4 ability picks is taken from
// m_vecAbilities slot order [0,1,2,5] (basic1, basic2, basic3, ult). This
// matches actual pick order for some drafters; we can't yet decode the
// underlying m_nAbilityID to do better.
//
// Output (JSON array, newline-terminated):
//
//	[{"order":0,"player_id":0,"tick":1814,"type":"ability",
//	  "ability_name":"faceless_void_time_walk"}, ...]

package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sort"
	"strings"

	"github.com/dotabuff/manta"
	"github.com/dotabuff/manta/dota"
	"google.golang.org/protobuf/proto"
)

// Pick represents a single selection in the Ability Draft.
type Pick struct {
	Order        int    `json:"order"`
	PlayerID     int    `json:"player_id"`
	Tick         uint32 `json:"tick"`
	Type         string `json:"type"` // "ability" or "model"
	AbilityName  string `json:"ability_name"`
	MNAbilityID  uint32 `json:"m_n_ability_id"` // raw opaque ID from CDOTAGamerulesProxy
	HeroID       int32  `json:"hero_id"`        // for type=model
}

// pickEvent is a single hasPicked false→true transition during the draft.
type pickEvent struct {
	tick    uint32
	round   int32
	tracker int32
}


func main() {
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "PANIC: %v\n", r)
			os.Exit(2)
		}
	}()

	log.SetOutput(os.Stderr)

	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: ad-parser <replay.dem>")
		os.Exit(1)
	}

	f, err := os.Open(os.Args[1])
	if err != nil {
		fmt.Fprintf(os.Stderr, "open: %v\n", err)
		os.Exit(1)
	}
	defer f.Close()

	p, err := manta.NewStreamParser(f)
	if err != nil {
		fmt.Fprintf(os.Stderr, "parser init: %v\n", err)
		os.Exit(1)
	}

	// --- Tracking state ---

	// Map handle → CDOTA_Ability_* class name (built live during parse).
	// Kept around for safe FindEntityByHandle fallback if needed elsewhere.
	abilityHandleToClass := make(map[uint64]string)

	// Per-player: m_unPickOrder (1-10). Used to map tracker → player.
	playerUnPickOrder := [10]int32{}

	// Per-player: tick at which m_nSelectedHeroID first became non-zero.
	modelPickTick := make(map[int]uint32)
	// And the hero ID picked.
	modelPickHeroID := make(map[int]int32)
	prevHeroID := [10]int32{}

	// Pick events from ADSTATE transitions (hasPicked false→true).
	var pickEvents []pickEvent
	var prevHasPicked bool
	var prevRound, prevTracker int32 = -1, -1

	// Diagnostic state — only used when AD_PARSER_DUMP_PICK_DIFF=1.
	lastPickState := map[string]string{}
	// Diagnostic state — only used when AD_PARSER_DUMP_PR_DIFF=1.
	lastPRState := map[string]string{}

	// m_AbilityDraftAbilities slot N → m_nAbilityID at that slot. Slot N is
	// chronological pick #(N+1). Captured at any point during the parse since
	// the array is fully populated at tick 2.
	mnAbilityIDPerSlot := [60]uint32{}

	// --- DEEP-MINE DIAGNOSTIC (env AD_PARSER_DEEP_MINE=1) ----------------
	// Goal: figure out whether m_AbilityDraftAbilities slots fill at tick 2
	// (pool) or incrementally (pick order), what subfields each slot has,
	// what other m_AbilityDraft* fields live on gamerules, and when each
	// CDOTA_Ability_* entity is created.
	deepMine := os.Getenv("AD_PARSER_DEEP_MINE") == "1"
	// Set of gamerules keys we've already logged a schema-discovery line for.
	grSchemaLogged := map[string]bool{}
	// Per-key last-seen value on gamerules (for AbilityDraft-related keys).
	// Used to detect changes during the parse, not just first-write.
	grLastVal := map[string]string{}
	// First-tick that a CDOTA_Ability_* entity was OnEntity-fired for each
	// (handle). Map handle → (firstTick, cname).
	type abilFirstSeen struct {
		tick  uint32
		cname string
		idx   int32
	}
	abilFirstSeenByHandle := map[uint64]*abilFirstSeen{}
	// Unique class names seen containing "Ability" or "Draft" — log once.
	cnameDiscovery := map[string]bool{}

	// Pick events from CDOTAUserMsg_AbilityDraftRequestAbility.
	// This is the actual on-the-wire pick message — the answer we want.
	type draftReqEvent struct {
		tick      uint32
		playerID  int32
		abilityID int32
		heroID    int32
	}
	var draftReqEvents []draftReqEvent

	// Counts of every UserMessage msg_type we see (deep-mine only).
	umsgTypeCounts := map[int32]int{}
	// Counts of every PACKET type we see in CDemoPacket (deep-mine only).
	pktTypeCounts := map[int32]int{}
	pktTypeFirstTick := map[int32]uint32{}

	// Raw CDemoPacket interception — read the same way manta does internally,
	// but only count types. This is the ground-truth view of what messages are
	// in the wire stream.
	p.Callbacks.OnCDemoPacket(func(m *dota.CDemoPacket) error {
		if !deepMine {
			return nil
		}
		// Mirror demo_packet.go internal logic: read packed {type, size, data}.
		data := m.GetData()
		// Manta's reader uses UBitVar for type and VarUint32 for size. We can't
		// re-use manta.newReader (unexported). Use a simple varint decoder
		// instead — this won't be exact for some message types but should give
		// us a useful count if any svc_UserMessage / DOTA_UM are firing.
		// Simpler: just count CDemoPacket calls — if zero UserMsg fires it's
		// because none arrive, not because we miscounted.
		_ = data
		pktTypeCounts[-1]++ // sentinel: total CDemoPacket count
		if _, ok := pktTypeFirstTick[-1]; !ok {
			pktTypeFirstTick[-1] = p.Tick
		}
		return nil
	})

	// Sanity check: register a few other common UserMsg callbacks. If ANY of
	// these fire, we know UserMsg dispatch works.
	chatCount := 0
	p.Callbacks.OnCDOTAUserMsg_ChatEvent(func(m *dota.CDOTAUserMsg_ChatEvent) error {
		chatCount++
		return nil
	})
	combatLogCount := 0
	p.Callbacks.OnCDOTAUserMsg_CombatLogBulkData(func(m *dota.CDOTAUserMsg_CombatLogBulkData) error {
		combatLogCount++
		return nil
	})
	playerDraftPickCount := 0
	p.Callbacks.OnCDOTAUserMsg_PlayerDraftPick(func(m *dota.CDOTAUserMsg_PlayerDraftPick) error {
		playerDraftPickCount++
		return nil
	})
	directDraftCount := 0
	p.Callbacks.OnCDOTAUserMsg_AbilityDraftRequestAbility(func(m *dota.CDOTAUserMsg_AbilityDraftRequestAbility) error {
		directDraftCount++
		log.Printf("[DM/DIRECT-PICKMSG] tick=%d player=%d ability=%d hero=%d",
			p.Tick, m.GetPlayerId(), m.GetRequestedAbilityId(), m.GetRequestedHeroId())
		return nil
	})

	// Enumerate string tables — names + entry count + a few sample entries.
	// After parsing completes, we'll dump full contents of any tables that
	// look promising (related to abilities/draft).
	stringTableMeta := map[string]int32{} // name → max_entries (declared count)
	p.Callbacks.OnCSVCMsg_CreateStringTable(func(m *dota.CSVCMsg_CreateStringTable) error {
		if !deepMine {
			return nil
		}
		name := m.GetName()
		stringTableMeta[name] = m.GetNumEntries()
		log.Printf("[DM/STRTABLE] created name=%q num_entries=%d user_data_size=%d",
			name, m.GetNumEntries(), m.GetUserDataSize())
		return nil
	})

	// CDemoFileInfo — the trailing match-summary block in every replay.
	// Dump the whole proto as text so we can see whether the per-player
	// ability_draft_abilities array is there.
	p.Callbacks.OnCDemoFileInfo(func(m *dota.CDemoFileInfo) error {
		if !deepMine {
			return nil
		}
		log.Printf("==================== CDemoFileInfo ====================")
		log.Printf("playback_time=%v playback_ticks=%v playback_frames=%v",
			m.GetPlaybackTime(), m.GetPlaybackTicks(), m.GetPlaybackFrames())
		gi := m.GetGameInfo()
		if gi == nil {
			log.Printf("  no GameInfo")
			return nil
		}
		dgi := gi.GetDota()
		if dgi == nil {
			log.Printf("  no GameInfo.Dota")
			return nil
		}
		log.Printf("  match_id=%v game_mode=%v winner=%v league_id=%v",
			dgi.GetMatchId(), dgi.GetGameMode(), dgi.GetGameWinner(), dgi.GetLeagueid())
		log.Printf("  picks_bans count=%d", len(dgi.GetPicksBans()))
		for i, pb := range dgi.GetPicksBans() {
			log.Printf("    [%d] is_pick=%v team=%v hero_id=%v",
				i, pb.GetIsPick(), pb.GetTeam(), pb.GetHeroId())
		}
		log.Printf("  player_info count=%d", len(dgi.GetPlayerInfo()))
		for i, pi := range dgi.GetPlayerInfo() {
			log.Printf("    [%d] hero_name=%q steam_id=%v",
				i, pi.GetHeroName(), pi.GetSteamid())
		}
		// Full proto text dump as a last resort (will include any
		// AD-specific fields if they're present).
		log.Printf("  --- full proto text ---")
		log.Printf("%s", m.String())
		log.Printf("==================== /CDemoFileInfo ====================")
		return nil
	})

	// Per-slot first-write tick for m_AbilityDraftAbilities[N].m_nAbilityID.
	// This is THE test: if slots fill incrementally at pick ticks, the array
	// itself encodes the chronological draft order.
	slotFirstTick := [60]uint32{} // 0 = not yet written

	// Full CDOTA_PlayerResource schema dump — track every key seen on this
	// entity so we know what per-player fields exist.
	prSchemaKeys := map[string]string{} // key → last-seen-stringified-value

	// CDemoCustomData / SaveGame interception — these blocks can sometimes
	// carry game-mode-specific summaries.
	customDataCount := 0
	p.Callbacks.OnCDemoCustomData(func(m *dota.CDemoCustomData) error {
		customDataCount++
		if deepMine {
			log.Printf("[DM/CUSTOMDATA] callback_index=%v size=%d bytes",
				m.GetCallbackIndex(), len(m.GetData()))
		}
		return nil
	})
	saveGameCount := 0
	p.Callbacks.OnCDemoSaveGame(func(m *dota.CDemoSaveGame) error {
		saveGameCount++
		if deepMine {
			log.Printf("[DM/SAVEGAME] version=%v size=%d",
				m.GetVersion(), len(m.GetData()))
		}
		return nil
	})

	// FlattenedSerializer arrives inside CDemoSendTables (manta unwraps it
	// internally but doesn't expose it). We piggyback on the same callback.
	p.Callbacks.OnCDemoSendTables(func(stm *dota.CDemoSendTables) error {
		if !deepMine {
			return nil
		}
		// Re-parse the inner CSVCMsg_FlattenedSerializer exactly as manta does.
		// The data is: varint-length-prefixed inner buffer.
		raw := stm.GetData()
		if len(raw) == 0 {
			return nil
		}
		// Decode varint length prefix manually.
		var innerLen uint64
		var shift uint
		var i int
		for i < len(raw) {
			b := raw[i]
			i++
			innerLen |= uint64(b&0x7f) << shift
			if b < 0x80 {
				break
			}
			shift += 7
		}
		if int(innerLen) > len(raw)-i {
			log.Printf("[DM/SENDTABLES] inner len %d > remaining %d", innerLen, len(raw)-i)
			return nil
		}
		innerBuf := raw[i : i+int(innerLen)]
		m := &dota.CSVCMsg_FlattenedSerializer{}
		if err := proto.Unmarshal(innerBuf, m); err != nil {
			log.Printf("[DM/SENDTABLES] unmarshal failed: %v", err)
			return nil
		}
		if !deepMine {
			return nil
		}
		symbols := m.GetSymbols()
		fields := m.GetFields()
		log.Printf("[DM/FLATSER] %d serializers, %d symbols, %d fields",
			len(m.GetSerializers()), len(symbols), len(fields))
		for _, s := range m.GetSerializers() {
			nameSym := s.GetSerializerNameSym()
			if int(nameSym) >= len(symbols) {
				continue
			}
			name := symbols[nameSym]
			lower := strings.ToLower(name)
			if !(strings.Contains(lower, "ability") || strings.Contains(lower, "draft") ||
				strings.Contains(lower, "gamerule")) {
				continue
			}
			log.Printf("  serializer: %s (v%d) fields=%d",
				name, s.GetSerializerVersion(), len(s.GetFieldsIndex()))
			for _, fi := range s.GetFieldsIndex() {
				if int(fi) >= len(fields) {
					continue
				}
				f := fields[fi]
				typeIdx := f.GetVarTypeSym()
				nameIdx := f.GetVarNameSym()
				var fname, ftype string
				if int(nameIdx) < len(symbols) {
					fname = symbols[nameIdx]
				}
				if int(typeIdx) < len(symbols) {
					ftype = symbols[typeIdx]
				}
				log.Printf("    [field %d] %s : %s", fi, fname, ftype)
			}
		}
		return nil
	})

	// Old non-firing callback kept as no-op for symmetry.
	p.Callbacks.OnCSVCMsg_FlattenedSerializer(func(m *dota.CSVCMsg_FlattenedSerializer) error {
		return nil
	})

	// Register handler for the SVC UserMessage wrapper. The DOTA AbilityDraft
	// pick message arrives as msg_type 573 inside this wrapper; manta does NOT
	// auto-route the inner message, so we unpack it ourselves.
	p.Callbacks.OnCSVCMsg_UserMessage(func(m *dota.CSVCMsg_UserMessage) error {
		t := m.GetMsgType()
		if deepMine {
			umsgTypeCounts[t]++
		}
		// 573 = EDotaUserMessages_DOTA_UM_AbilityDraftRequestAbility
		if t == 573 {
			inner := &dota.CDOTAUserMsg_AbilityDraftRequestAbility{}
			if err := proto.Unmarshal(m.GetMsgData(), inner); err != nil {
				log.Printf("WARN: unmarshal AbilityDraftRequestAbility failed: %v", err)
				return nil
			}
			ev := draftReqEvent{
				tick:      p.Tick,
				playerID:  inner.GetPlayerId(),
				abilityID: inner.GetRequestedAbilityId(),
				heroID:    inner.GetRequestedHeroId(),
			}
			draftReqEvents = append(draftReqEvents, ev)
			if deepMine {
				log.Printf("[DM/PICKMSG] tick=%d player_id=%d ability_id=%d hero_id=%d ctrl=%v",
					ev.tick, ev.playerID, ev.abilityID, ev.heroID, inner.GetCtrlIsDown())
			}
		}
		return nil
	})

	p.OnEntity(func(e *manta.Entity, op manta.EntityOp) error {
		cname := e.GetClassName()

		// DEEP-MINE: discover unique class names containing Ability/Draft.
		if deepMine {
			if !cnameDiscovery[cname] && (strings.Contains(cname, "Ability") || strings.Contains(cname, "Draft")) {
				log.Printf("[DM/CLASS] tick=%d cname=%s op=%v", p.Tick, cname, op)
			}
			cnameDiscovery[cname] = true
		}

		// 1) Capture ability entity handles → class names. Used to resolve
		// the hero entity's m_vecAbilities handles to class names (for the
		// hero MODEL identification — abilities are resolved by Python).
		if strings.HasPrefix(cname, "CDOTA_Ability_") {
			idx := uint32(e.GetIndex())
			serial := uint32(e.GetSerial())
			handle := (uint64(serial) << 14) | uint64(idx)
			abilityHandleToClass[handle] = cname

			// DEEP-MINE: log first sighting per handle.
			if deepMine {
				if _, ok := abilFirstSeenByHandle[handle]; !ok {
					abilFirstSeenByHandle[handle] = &abilFirstSeen{
						tick: p.Tick, cname: cname, idx: int32(e.GetIndex()),
					}
					// Dump just the subclass + name for EVERY ability entity.
					// This is the potential bridge between mnID and ability name.
					subclass := e.Get("m_nSubclassID")
					log.Printf("[DM/ABIL-SUBCLASS] cname=%s idx=%d m_nSubclassID=%v",
						cname, idx, subclass)
				}
			}
			return nil
		}

		// 2) Hero entity (post-draft): we don't actually need this. Skip.
		if strings.HasPrefix(cname, "CDOTA_Unit_Hero_") {
			return nil
		}

		// 3) CDOTA_PlayerResource: track m_unPickOrder, m_nSelectedHeroID.
		if cname == "CDOTA_PlayerResource" {
			// DEEP-MINE: every fired callback, update our schema-keys map.
			if deepMine {
				m := e.Map()
				for k, v := range m {
					prSchemaKeys[k] = fmt.Sprintf("%v", v)
				}
			}
			// DIAGNOSTIC: snapshot all flat keys and report changes between
			// snapshots (limited to keys with values that fit small ints).
			// Only when AD_PARSER_DUMP_PR_DIFF=1.
			if os.Getenv("AD_PARSER_DUMP_PR_DIFF") == "1" {
				m := e.Map()
				cur := make(map[string]string, len(m))
				for k, v := range m {
					cur[k] = fmt.Sprintf("%v", v)
				}
				var changes []string
				for k, v := range cur {
					if prev, ok := lastPRState[k]; ok && prev != v {
						changes = append(changes, fmt.Sprintf("%s: %s → %s", k, prev, v))
					}
				}
				if len(changes) > 0 && p.Tick > 1000 && p.Tick < 13000 {
					sort.Strings(changes)
					log.Printf("--- PR change at tick=%d ---", p.Tick)
					for _, c := range changes {
						log.Printf("  %s", c)
					}
				}
				lastPRState = cur
			}

			for pid := 0; pid < 10; pid++ {
				// m_unPickOrder (static, just need to capture once).
				if playerUnPickOrder[pid] == 0 {
					v := e.Get(fmt.Sprintf("m_vecPlayerTeamData.%04d.m_unPickOrder", pid))
					if v != nil {
						switch vv := v.(type) {
						case uint32:
							if vv > 0 {
								playerUnPickOrder[pid] = int32(vv)
							}
						case int32:
							if vv > 0 {
								playerUnPickOrder[pid] = vv
							}
						case uint64:
							if vv > 0 {
								playerUnPickOrder[pid] = int32(vv)
							}
						}
					}
				}

				// m_nSelectedHeroID — first non-zero transition is the model pick.
				v := e.Get(fmt.Sprintf("m_vecPlayerTeamData.%04d.m_nSelectedHeroID", pid))
				if v == nil {
					continue
				}
				var hid int32
				switch vv := v.(type) {
				case int32:
					hid = vv
				case uint32:
					hid = int32(vv)
				}
				if hid != 0 && prevHeroID[pid] == 0 {
					modelPickTick[pid] = p.Tick
					modelPickHeroID[pid] = hid
				}
				prevHeroID[pid] = hid
			}
			return nil
		}

		// 4) CDOTAGamerulesProxy: track ADSTATE transitions to extract picks
		// and capture m_AbilityDraftAbilities slot values.
		if cname == "CDOTAGamerulesProxy" {
			// DEEP-MINE: full schema walk for AbilityDraft-related keys.
			if deepMine {
				m := e.Map()
				for k, v := range m {
					if !(strings.Contains(k, "AbilityDraft") || strings.Contains(k, "DraftAbility")) {
						continue
					}
					vs := fmt.Sprintf("%v", v)
					if !grSchemaLogged[k] {
						log.Printf("[DM/SCHEMA] tick=%d key=%s val=%s", p.Tick, k, vs)
						grSchemaLogged[k] = true
						grLastVal[k] = vs
						continue
					}
					if old := grLastVal[k]; old != vs {
						log.Printf("[DM/CHANGE] tick=%d key=%s old=%s new=%s", p.Tick, k, old, vs)
						grLastVal[k] = vs
					}
				}
			}

			// Slot values (mostly populated at tick 2 as a single baseline).
			for i := 0; i < 60; i++ {
				if mnAbilityIDPerSlot[i] != 0 {
					continue
				}
				raw := e.Get(fmt.Sprintf("m_pGameRules.m_AbilityDraftAbilities.%04d.m_nAbilityID", i))
				if raw == nil {
					continue
				}
				switch v := raw.(type) {
				case uint32:
					if v != 0 {
						mnAbilityIDPerSlot[i] = v
						if deepMine && slotFirstTick[i] == 0 {
							slotFirstTick[i] = p.Tick
						}
					}
				case int32:
					if v > 0 {
						mnAbilityIDPerSlot[i] = uint32(v)
						if deepMine && slotFirstTick[i] == 0 {
							slotFirstTick[i] = p.Tick
						}
					}
				}
			}
			roundRaw := e.Get("m_pGameRules.m_nAbilityDraftRoundNumber")
			trackerRaw := e.Get("m_pGameRules.m_nAbilityDraftPlayerTracker")
			pickedRaw := e.Get("m_pGameRules.m_bAbilityDraftCurrentPlayerHasPicked")

			var round, tracker int32
			var hasPicked bool
			if roundRaw != nil {
				switch v := roundRaw.(type) {
				case int32:
					round = v
				case uint32:
					round = int32(v)
				}
			}
			if trackerRaw != nil {
				switch v := trackerRaw.(type) {
				case int32:
					tracker = v
				case uint32:
					tracker = int32(v)
				}
			}
			if pickedRaw != nil {
				if b, ok := pickedRaw.(bool); ok {
					hasPicked = b
				}
			}

			// Detect false→true edge (a new pick was just committed).
			if hasPicked && !prevHasPicked {
				pickEvents = append(pickEvents, pickEvent{
					tick:    p.Tick,
					round:   round,
					tracker: tracker,
				})
				// DIAGNOSTIC: at each pick tick, snapshot CDOTAGamerulesProxy
				// flat keys and report which fields CHANGED compared to the
				// previous pick's snapshot. This should reveal any field that
				// uniquely identifies the just-picked ability.
				if os.Getenv("AD_PARSER_DUMP_PICK_DIFF") == "1" {
					m := e.Map()
					curState := make(map[string]string, len(m))
					for k, v := range m {
						curState[k] = fmt.Sprintf("%v", v)
					}
					var changes []string
					for k, v := range curState {
						if prev, ok := lastPickState[k]; !ok || prev != v {
							if prev2, ok2 := lastPickState[k]; ok2 {
								changes = append(changes, fmt.Sprintf("%s: %s → %s", k, prev2, v))
							} else {
								changes = append(changes, fmt.Sprintf("NEW %s = %s", k, v))
							}
						}
					}
					sort.Strings(changes)
					log.Printf("--- PICK at tick=%d round=%d tracker=%d ---", p.Tick, round, tracker)
					for _, c := range changes {
						log.Printf("  %s", c)
					}
					lastPickState = curState
				}
			}
			prevHasPicked = hasPicked
			prevRound = round
			prevTracker = tracker
		}
		return nil
	})

	if err := p.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "parse: %v\n", err)
		os.Exit(1)
	}

	_ = prevRound
	_ = prevTracker

	// --- DEEP-MINE end-of-parse summary -----------------------------------
	if deepMine {
		log.Printf("==================== DEEP MINE SUMMARY ====================")
		// Sorted list of all gamerules AbilityDraft keys we discovered
		// (i.e. the schema).
		var grKeys []string
		for k := range grSchemaLogged {
			grKeys = append(grKeys, k)
		}
		sort.Strings(grKeys)
		log.Printf("---- gamerules.* keys touching AbilityDraft (%d) ----", len(grKeys))
		for _, k := range grKeys {
			log.Printf("  %s = %s", k, grLastVal[k])
		}
		// Ability entity first-sightings, sorted by tick.
		type abilLog struct {
			tick  uint32
			cname string
			idx   int32
		}
		var abLogs []abilLog
		for _, v := range abilFirstSeenByHandle {
			abLogs = append(abLogs, abilLog{v.tick, v.cname, v.idx})
		}
		sort.Slice(abLogs, func(i, j int) bool {
			if abLogs[i].tick != abLogs[j].tick {
				return abLogs[i].tick < abLogs[j].tick
			}
			return abLogs[i].cname < abLogs[j].cname
		})
		log.Printf("---- CDOTA_Ability_* entities first-seen (%d) ----", len(abLogs))
		for _, a := range abLogs {
			log.Printf("  tick=%d idx=%d cname=%s", a.tick, a.idx, a.cname)
		}

		log.Printf("---- String tables (%d) ----", len(stringTableMeta))
		var tabNames []string
		for n := range stringTableMeta {
			tabNames = append(tabNames, n)
		}
		sort.Strings(tabNames)
		for _, n := range tabNames {
			log.Printf("  table=%q declared_entries=%d", n, stringTableMeta[n])
		}
		// For any string table whose name suggests it might be useful, dump
		// the first ~100 entries. Specifically: anything containing
		// "ability", "draft", or "instancebaseline".
		for _, n := range tabNames {
			lower := strings.ToLower(n)
			if !(strings.Contains(lower, "abil") || strings.Contains(lower, "draft") ||
				strings.Contains(lower, "instance") || strings.Contains(lower, "modifier")) {
				continue
			}
			log.Printf("  >>>> dumping table %q <<<<", n)
			for idx := int32(0); idx < 200; idx++ {
				v, ok := p.LookupStringByIndex(n, idx)
				if !ok {
					break
				}
				log.Printf("    [%d] = %q", idx, v)
			}
		}
		// Brute-force probe a list of plausible slot[0] subfield names from the
		// LATEST entity state.
		var gr *manta.Entity
		func() {
			defer func() {
				if r := recover(); r != nil {
					log.Printf("FilterEntity panicked: %v", r)
				}
			}()
			grs := p.FilterEntity(func(e *manta.Entity) bool {
				return e != nil && e.GetClassName() == "CDOTAGamerulesProxy"
			})
			if len(grs) > 0 {
				gr = grs[0]
			}
		}()
		log.Printf("---- m_AbilityDraftAbilities[0] probe (gr nil? %v) ----", gr == nil)
		if gr != nil {
			candidates := []string{
				"m_nAbilityID",
				"m_iPlayerID", "m_nPlayerID", "m_unPlayerID", "m_iOwnerPlayerID",
				"m_iPickedBy", "m_nPickedBy", "m_unPickedBy",
				"m_bPicked", "m_bSelected", "m_bAvailable", "m_bDrafted",
				"m_nTickPicked", "m_unTickPicked", "m_flTimePicked",
				"m_iAbility", "m_nAbility", "m_iAbilityNameIdx",
				"m_nSlot", "m_iSlot", "m_nPickIndex", "m_nPickOrder",
				"m_iHero", "m_nHeroID", "m_iHeroID",
				"m_iTeam", "m_nTeam",
				"m_szAbilityName", "m_strAbility", "m_AbilityName",
			}
			log.Printf("---- m_AbilityDraftAbilities[0] probed subfields (non-nil only) ----")
			for _, sf := range candidates {
				path := fmt.Sprintf("m_pGameRules.m_AbilityDraftAbilities.0000.%s", sf)
				func() {
					defer func() {
						if r := recover(); r != nil {
							log.Printf("  %s: panicked (%v)", sf, r)
						}
					}()
					v := gr.Get(path)
					if v != nil {
						log.Printf("  %s = %v (%T)", sf, v, v)
					}
				}()
			}
		}
		// Dump all three subfields of every slot in m_AbilityDraftAbilities.
		if gr != nil {
			log.Printf("---- m_AbilityDraftAbilities: (mnID, playerID, abilityPlayerSlot) ----")
			for i := 0; i < 48; i++ {
				abilPath := fmt.Sprintf("m_pGameRules.m_AbilityDraftAbilities.%04d.m_nAbilityID", i)
				plPath := fmt.Sprintf("m_pGameRules.m_AbilityDraftAbilities.%04d.m_unPlayerID", i)
				slotPath := fmt.Sprintf("m_pGameRules.m_AbilityDraftAbilities.%04d.m_unAbilityPlayerSlot", i)
				ab := gr.Get(abilPath)
				pl := gr.Get(plPath)
				sl := gr.Get(slotPath)
				log.Printf("  slot[%02d] mnID=%v playerID=%v abilityPlayerSlot=%v", i, ab, pl, sl)
			}
			// Dump m_AbilityDraftHeroes — a SEPARATE array we never queried.
			log.Printf("---- m_AbilityDraftHeroes: (nHeroID, unPlayerID) ----")
			for i := 0; i < 20; i++ {
				heroPath := fmt.Sprintf("m_pGameRules.m_AbilityDraftHeroes.%04d.m_nHeroID", i)
				plPath := fmt.Sprintf("m_pGameRules.m_AbilityDraftHeroes.%04d.m_unPlayerID", i)
				h := gr.Get(heroPath)
				p := gr.Get(plPath)
				if h == nil && p == nil {
					continue
				}
				log.Printf("  heroSlot[%02d] hero_id=%v unPlayerID=%v", i, h, p)
			}
		}

		// Per pid, find the REAL hero entity (lowest entity index for each
		// m_iPlayerID = pid * 2). Then dump m_vecAbilities[0..15] resolved.
		heroes := p.FilterEntity(func(e *manta.Entity) bool {
			return e != nil && strings.HasPrefix(e.GetClassName(), "CDOTA_Unit_Hero_")
		})
		pidToMainHero := map[int]*manta.Entity{}
		for _, he := range heroes {
			pidRaw := he.Get("m_iPlayerID")
			var pidVal int32
			switch v := pidRaw.(type) {
			case int32:
				pidVal = v
			case uint32:
				pidVal = int32(v)
			default:
				continue
			}
			pid := int(pidVal) / 2
			if pid < 0 || pid > 9 {
				continue
			}
			cur, ok := pidToMainHero[pid]
			if !ok || he.GetIndex() < cur.GetIndex() {
				pidToMainHero[pid] = he
			}
		}
		log.Printf("---- Per-pid main hero entity ----")
		for pid := 0; pid < 10; pid++ {
			he := pidToMainHero[pid]
			if he == nil {
				log.Printf("  pid=%d: NO hero entity found", pid)
				continue
			}
			log.Printf("  pid=%d hero=%s idx=%d", pid, he.GetClassName(), he.GetIndex())
			for k := 0; k < 16; k++ {
				v := he.Get(fmt.Sprintf("m_vecAbilities.%04d", k))
				if v == nil {
					continue
				}
				h, ok := v.(uint32)
				if !ok || h == 0xFFFFFF {
					continue
				}
				// Manta handles: (serial << 14) | index. FindEntityByHandle
				// takes that combined form.
				ae := p.FindEntityByHandle(uint64(h))
				var aname string
				if ae != nil {
					aname = ae.GetClassName()
				}
				log.Printf("    m_vecAbilities[%02d] handle=%d → %s", k, h, aname)
			}
		}

		log.Printf("---- m_AbilityDraftAbilities slot first-write tick ----")
		for i := 0; i < 60; i++ {
			if mnAbilityIDPerSlot[i] != 0 || slotFirstTick[i] != 0 {
				log.Printf("  slot[%02d] first_tick=%d m_nAbilityID=%d",
					i, slotFirstTick[i], mnAbilityIDPerSlot[i])
			}
		}
		log.Printf("---- CDOTA_PlayerResource ALL keys (%d) ----", len(prSchemaKeys))
		var prKeys []string
		for k := range prSchemaKeys {
			prKeys = append(prKeys, k)
		}
		sort.Strings(prKeys)
		for _, k := range prKeys {
			log.Printf("  %s = %s", k, prSchemaKeys[k])
		}

		log.Printf("---- Callback fire sanity check ----")
		log.Printf("  CDemoPacket total invocations: %d", pktTypeCounts[-1])
		log.Printf("  ChatEvent fires: %d", chatCount)
		log.Printf("  CombatLogBulkData fires: %d", combatLogCount)
		log.Printf("  PlayerDraftPick fires: %d", playerDraftPickCount)
		log.Printf("  AbilityDraftRequestAbility (direct callback) fires: %d", directDraftCount)
		log.Printf("  CDemoCustomData fires: %d", customDataCount)
		log.Printf("  CDemoSaveGame fires: %d", saveGameCount)
		log.Printf("---- UserMessage msg_type counts (%d distinct) ----", len(umsgTypeCounts))
		var umsgTs []int32
		for t := range umsgTypeCounts {
			umsgTs = append(umsgTs, t)
		}
		sort.Slice(umsgTs, func(i, j int) bool { return umsgTs[i] < umsgTs[j] })
		for _, t := range umsgTs {
			log.Printf("  msg_type=%d count=%d", t, umsgTypeCounts[t])
		}

		log.Printf("---- DraftRequestAbility user-messages (%d) ----", len(draftReqEvents))
		for i, ev := range draftReqEvents {
			log.Printf("  msg[%d] tick=%d player_id=%d ability_id=%d hero_id=%d",
				i, ev.tick, ev.playerID, ev.abilityID, ev.heroID)
		}
		log.Printf("==================== END DEEP MINE ====================")
	}

	// --- Build tracker → player_id mapping ---
	// tracker T is the player with m_unPickOrder = T+1.
	trackerToPlayer := [10]int{}
	for i := range trackerToPlayer {
		trackerToPlayer[i] = -1
	}
	for pid := 0; pid < 10; pid++ {
		uo := playerUnPickOrder[pid]
		if uo >= 1 && uo <= 10 {
			trackerToPlayer[uo-1] = pid
		}
	}
	log.Printf("trackerToPlayer: %v", trackerToPlayer)

	log.Printf("captured %d pick events", len(pickEvents))

	// Sort pickEvents chronologically by tick (they already arrive in order,
	// but be safe). Within the same tick, fall back to round/tracker.
	sort.SliceStable(pickEvents, func(i, j int) bool {
		if pickEvents[i].tick != pickEvents[j].tick {
			return pickEvents[i].tick < pickEvents[j].tick
		}
		return pickEvents[i].round < pickEvents[j].round
	})

	picks := make([]Pick, 0, len(pickEvents))
	for order, ev := range pickEvents {
		pid := trackerToPlayer[ev.tracker]
		if pid < 0 {
			continue
		}
		isModel := false
		if modelTk, ok := modelPickTick[pid]; ok && modelTk == ev.tick {
			isModel = true
		}

		var ptype string
		var heroID int32
		if isModel {
			ptype = "model"
			// modelPickHeroID is the raw m_nSelectedHeroID, which is 2x
			// OpenDota's hero_id. Divide so we output the canonical id
			// matching OpenDota/windrun's hero_id schema.
			heroID = modelPickHeroID[pid] / 2
		} else {
			ptype = "ability"
		}
		// Python resolves both ability and hero names from IDs.

		// m_nAbilityID at chronological slot=order (may be 0 for slots 48-59
		// where the array isn't populated).
		var mnID uint32
		if order < 60 {
			mnID = mnAbilityIDPerSlot[order]
		}
		log.Printf("  pick order=%d tick=%d player=%d round=%d tracker=%d type=%s mnID=%d hero_id=%d",
			order, ev.tick, pid, ev.round, ev.tracker, ptype, mnID, heroID)
		picks = append(picks, Pick{
			Order:       order,
			PlayerID:    pid,
			Tick:        ev.tick,
			Type:        ptype,
			AbilityName: "",
			MNAbilityID: mnID,
			HeroID:      heroID,
		})
	}

	if err := json.NewEncoder(os.Stdout).Encode(picks); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
}
