-- Cantrip Tailor - AC:Unreal API 2 companion plugin.
--
-- Which pieces of looted armour, worn together, give the best cantrips? This
-- plugin is the in-game half of the answer; the working-out is done on your
-- desktop by the `cantrip-tailor` command that ships with it (see README.md).
--
--   1. DUMP tab   writes everything you are wearing or wielding, plus every
--                 piece of armour, clothing and jewellery in your packs, to
--                 JSON: slots, spells, requirements.
--   2. desktop    `cantrip-tailor` reads the dump, works out the best set and
--                 writes a plan.
--   3. PLAN tab   shows that plan. Each piece still in a pack has a button
--                 that SELECTS it - your inventory highlights it, you wear it -
--                 and the list ticks itself off as your inventory changes.
--
-- The SETTINGS tab is where you say what you want: the attack skill to build
-- for, how many points each cantrip is worth, which worn pieces to keep no
-- matter what, and so on. It is saved per character and the desktop command
-- reads it, so the command itself can be run with no options at all.
--
-- What it does in the game, and nothing else:
--   * appraises wearables whose spells the client hasn't seen yet ("Scan +
--     dump"): in the background, one a second - the companion limit - without
--     touching your selection or the examine panel. "Dump now" skips this.
--   * selects an item when you press its button. A companion plugin cannot
--     equip, move, use or drop anything, and this one doesn't try.
--
-- Permissions: data (read the inventory), identify (those appraisals), ui,
-- profiles (read and write files in this plugin's OWN library only).
--
-- Files, all under Saved/ClientPlugins/Profiles/cantrip_tailor/Library/:
--   Dumps/gear_<character>_<n>.json   items, 20 per file
--   Dumps/gear_<character>.json       index, written last
--   Plans/plan_<character>.json       written by the desktop command, read here
--   Settings/settings_<character>.json  written here, read by the desktop command
-- Every file of one dump carries the same dump_id, so a reader can tell a
-- complete dump from a half-written or stale one.
--
-- Sandbox notes for anyone reading the code: there is no pcall, no Lua string
-- pattern functions, a bounded instruction budget per callback, and whatever
-- we hand back must be JSON-clean. So records are copied through clean(), and
-- the work is spread over callbacks: one appraisal or one 20-item file a tick.

local VERSION = "0.2.0"
local WEARABLE = 0x2 | 0x4 | 0x8   -- item types: armour (incl. shields), clothing, jewellery
local CONTAINER = 0x200            -- item type: packs
local CHUNK = 20                   -- items per file
local IDENTIFY_GAP = 1.1           -- seconds between appraisal requests
local RETRY_AFTER = 8              -- seconds before asking again about the same item
local MAX_TRIES = 3

local job = nil                    -- {phase="identify"} or {phase="write", ids=..., pos=..., ...}
local queue = {}                   -- profile save requests waiting to go out
local tries = {}                   -- item id (as text) -> {n=attempts, at=time of the last one}
local last_identify = -100
local files_ok, files_failed, files_expected = 0, 0, 0
local status = "Ready."
local last_error = ""
local last_summary = ""

local PLAN_POLL = 15               -- seconds between looks for a newer plan file
local plan = nil                   -- the plan table from the file, or nil
local plan_note = "Looking for a plan..."
local plan_asked = -100
local click_note = ""

local function band(a, b) return math.floor(a or 0) & math.floor(b or 0) end

local function finite(n) return n == n and n ~= math.huge and n ~= -math.huge end

-- GUIDs can arrive as floats; this gives a stable text key either way.
local function idkey(id) return string.format("%.0f", tonumber(id) or 0) end

-- Letters, digits, underscore and hyphen only: the profiles service rejects
-- anything else in a file name. (No gsub in the sandbox, hence the loop.)
local function slug(text)
  local out = {}
  for i = 1, math.min(#text, 40) do
    local b = string.byte(text, i)
    local ok = (b >= 48 and b <= 57) or (b >= 65 and b <= 90) or (b >= 97 and b <= 122) or b == 45 or b == 95
    out[#out + 1] = ok and string.char(b) or "_"
  end
  if #out == 0 then return "unknown" end
  return table.concat(out)
end

-- Deep copy that the host's serializer will accept: strings are capped (one
-- over-long inscription must not sink the whole file), non-finite numbers and
-- functions are dropped, and a table is emitted either as a dense array or as
-- a record with string keys - never a mix.
local function clean(v, depth)
  local tv = type(v)
  if tv == "string" then
    if #v > 600 then return string.sub(v, 1, 600) end
    return v
  end
  if tv == "boolean" then return v end
  if tv == "number" then
    if finite(v) then return v end
    return nil
  end
  if tv ~= "table" or depth <= 0 then return nil end
  local n, count = #v, 0
  for _ in pairs(v) do count = count + 1 end
  local out = {}
  if n > 0 and count == n then
    for i = 1, n do
      local c = clean(v[i], depth - 1)
      if c ~= nil then out[#out + 1] = c end
    end
  else
    for k, x in pairs(v) do
      local c = clean(x, depth - 1)
      if c ~= nil then out[tostring(k)] = c end
    end
  end
  return out
end

-- What we dump: anything being worn or wielded, and any wearable in the packs.
local function is_gear(item)
  return item.equipped == true or band(item.type, WEARABLE) ~= 0
end

local function short(text, n)
  text = tostring(text or "")
  if #text > n then return string.sub(text, 1, n - 3) .. "..." end
  return text
end

local function ask_for_plan(s, now)
  queue[#queue + 1] = { id = "plan", service = "profiles", operation = "read", group = "Plans",
                        name = "plan_" .. slug(tostring(s.character_name or "")) }
  plan_asked = now
end

local function on_plan_result(s, e)
  local data = e.data
  if not e.ok or type(data) ~= "table" or data.kind ~= "gear_plan" then
    -- A failed look (no file yet, or a busy service) never throws away a plan we already have.
    if not plan then plan_note = "No plan for " .. tostring(s.character_name or "this character") .. " yet." end
    return
  end
  if tostring(data.character or "") ~= tostring(s.character_name or "") then
    plan = nil
    plan_note = "The plan file is for " .. tostring(data.character) .. ", not this character."
    return
  end
  if not plan or plan.plan_id ~= data.plan_id then click_note = "" end
  plan = data
end

-- The Plan tab. Everything about a piece's state comes from the live
-- inventory, so the list is right even if the plan is a few swaps old.
local function plan_rows(s, by_id, inventory)
  local rows = {}
  if not plan then
    rows[#rows + 1] = { id = "p_none", text = plan_note, color = "yellow" }
    rows[#rows + 1] = { id = "p_how1", text = "1. Dump tab: Scan + dump." }
    rows[#rows + 1] = { id = "p_how2", text = "2. On your desktop, in the Cantrip Tailor folder:  uv run cantrip-tailor" }
    rows[#rows + 1] = { id = "p_how3", text = "3. The plan appears here within " .. PLAN_POLL .. " seconds (or press Reload plan)." }
    rows[#rows + 1] = { id = "reload", type = "button", text = "Reload plan" }
    return rows
  end

  local pack_no, n = {}, 0         -- pack id -> its position down the side of the inventory panel
  for _, item in ipairs(inventory) do
    if band(item.type, CONTAINER) ~= 0 then n = n + 1; pack_no[idkey(item.id)] = n end
  end
  local function where(live)
    if idkey(live.container) == idkey(s.player) or (tonumber(live.container) or 0) == 0 then return "main pack" end
    local k = pack_no[idkey(live.container)]
    return k and ("pack " .. k) or "a pack"
  end

  local on, todo, gone, first = 0, 0, 0, nil
  local pieces = {}
  for i, piece in ipairs(plan.wear or {}) do
    local live = by_id[idkey(piece.id)]
    local state, color, loc = "PUT ON", "yellow", ""
    if not live then state, color = "GONE", "red"; gone = gone + 1
    elseif live.equipped then state, color = "on", "green"; on = on + 1
    else
      todo = todo + 1
      loc = "  [" .. where(live) .. "]"
      -- "Select next" follows the plan's put-on order (pieces that raise
      -- Arcane Lore first, then rising requirement), not the paper doll.
      local rank = tonumber(piece.order) or i
      if not first or rank < first.rank then first = { piece = piece, live = live, rank = rank } end
    end
    local tip = short(tostring(piece.spells or ""), 300)
    if (piece.note or "") ~= "" then tip = tip .. "  !! " .. short(piece.note, 150) end
    local text = state .. "  " .. tostring(piece.name) .. loc .. "  -  " .. tostring(piece.gives or "")
    if (piece.note or "") ~= "" then text = text .. "   !! " .. piece.note end
    local button = { id = "wb" .. i, type = "button", text = short(piece.slot, 24), width = 150, tooltip = tip,
                     enabled = (live ~= nil and not live.equipped) }
    if live and not live.equipped then
      button.action, button.target, button.container = "select_item", live.id, live.container
    end
    pieces[#pieces + 1] = { id = "w" .. i, type = "row", children = {
      button, { id = "wt" .. i, text = short(text, 400), color = color, tooltip = tip } } }
  end

  local off = {}
  for i, piece in ipairs(plan.take_off or {}) do
    local live = by_id[idkey(piece.id)]
    if live and live.equipped then
      off[#off + 1] = { id = "off" .. i, text = "TAKE OFF  " .. tostring(piece.name) .. "  (" .. tostring(piece.slot) .. ")", color = "red" }
    end
  end

  rows[#rows + 1] = { id = "p_title", text = tostring(plan.character) .. " - best set for " .. tostring(plan.attack) ..
    "  (score " .. string.format("%.0f", tonumber(plan.score) or 0) .. ")", bold = true }
  local summary = on .. " of " .. #pieces .. " on"
  if todo > 0 then summary = summary .. ",  " .. todo .. " to put on" end
  if #off > 0 then summary = summary .. ",  " .. #off .. " to take off first" end
  if gone > 0 then summary = summary .. ",  " .. gone .. " no longer in the inventory - dump and plan again" end
  if todo == 0 and #off == 0 and gone == 0 then summary = "All " .. #pieces .. " pieces are on. Done." end
  rows[#rows + 1] = { id = "p_sum", text = summary, color = (todo + #off + gone == 0) and "green" or "yellow" }
  if first then
    rows[#rows + 1] = { id = "next", type = "button",
      text = short("Select next:  " .. tostring(first.piece.name) .. "  [" .. where(first.live) .. "]", 120),
      action = "select_item", target = first.live.id, container = first.live.container }
  end
  if click_note ~= "" then rows[#rows + 1] = { id = "p_click", text = click_note, color = "red" } end
  for _, row in ipairs(off) do rows[#rows + 1] = row end
  for _, row in ipairs(pieces) do rows[#rows + 1] = row end

  local cover = {}
  for i, line in ipairs(plan.coverage or {}) do cover[#cover + 1] = { id = "c" .. i, text = short(line, 200) } end
  if #(plan.missing or {}) > 0 then
    cover[#cover + 1] = { id = "c_missing", text = short("Nothing for: " .. table.concat(plan.missing, ", "), 400), color = "yellow" }
  end
  rows[#rows + 1] = { id = "p_cover", type = "section", text = "What this set covers (" .. #(plan.coverage or {}) .. ")",
                      expanded = false, children = cover }
  if (plan.options or "") ~= "" then rows[#rows + 1] = { id = "p_opts", text = "Planned with: " .. short(plan.options, 200) } end
  rows[#rows + 1] = { id = "reload", type = "button", text = "Reload plan" }
  return rows
end

-- ---------------------------------------------------------------------------
-- Settings tab
-- ---------------------------------------------------------------------------
-- The numbers below are the DEFAULT priorities, and they are a copy of the
-- ones in the desktop command (src/cantrip_tailor/solver.py). The command is
-- the one that uses them; this copy exists only so the tab can show you what
-- you would get. The settings file stores just what you CHANGED, so a default
-- that is later improved reaches you without you having to reset anything.
-- (A test checks the two copies agree.)
local ATTACKS = {     -- key, name in the dropdown, its cantrip, the attributes behind the skill, skill id
  { key = "void", label = "Void Magic", cantrip = "Void Magic Aptitude", attrs = { "Focus", "Willpower" }, skill = 43 },
  { key = "war", label = "War Magic", cantrip = "War Magic Aptitude", attrs = { "Focus", "Willpower" }, skill = 34 },
  { key = "missile", label = "Missile Weapons", cantrip = "Missile Weapon Aptitude", attrs = { "Coordination" }, skill = 47 },
  { key = "heavy", label = "Heavy Weapons", cantrip = "Heavy Weapon Aptitude", attrs = { "Strength", "Coordination" }, skill = 44 },
  { key = "light", label = "Light Weapons", cantrip = "Light Weapon Aptitude", attrs = { "Strength", "Coordination" }, skill = 45 },
  { key = "finesse", label = "Finesse Weapons", cantrip = "Finesse Weapon Aptitude", attrs = { "Coordination", "Quickness" }, skill = 46 },
  { key = "two-handed", label = "Two Handed Combat", cantrip = "Two Handed Combat Aptitude", attrs = { "Strength", "Coordination" }, skill = 41 },
  { key = "summoning", label = "Summoning", cantrip = "Summoning Prowess", attrs = { "Endurance", "Willpower" } },
}
local AUTO = "Automatic (from your skills)"
local ATTACK_POINTS, WARD_POINTS, SUPPORT_POINTS = 100, 30, 15
local FIXED = {
  { "Creature Enchantment Aptitude", 90 }, { "Life Magic Aptitude", 80 }, { "Item Enchantment Aptitude", 70 },
  { "Invulnerability", 60 }, { "Magic Resistance", 60 }, { "Mana Conversion Prowess", 50 },
}
local WARDS = { "Acid Ward", "Bludgeoning Ward", "Flame Ward", "Frost Ward", "Piercing Ward", "Slashing Ward", "Storm Ward" }
local SUPPORT = { "Armor", "Endurance", "Impregnability", "Focus", "Willpower", "Coordination", "Quickness" }
-- Every other cantrip loot can carry, for the "add a cantrip" dropdown.
local KNOWN = {
   "Strength", "Endurance", "Coordination", "Quickness", "Focus", "Willpower", "Armor", "Impenetrability",
   "Acid Bane", "Bludgeoning Bane", "Flame Bane", "Frost Bane", "Piercing Bane", "Slashing Bane",
   "Storm Bane", "Alchemical Prowess", "Arcane Prowess", "Armor Tinkering Expertise", "Cooking Prowess",
   "Creature Enchantment Aptitude", "Deception Prowess", "Dirty Fighting Prowess", "Dual Wield Aptitude",
   "Fealty", "Finesse Weapon Aptitude", "Fletching Prowess", "Healing Prowess", "Heavy Weapon Aptitude",
   "Impregnability", "Invulnerability", "Item Enchantment Aptitude", "Item Tinkering Expertise",
   "Jumping Prowess", "Leadership", "Life Magic Aptitude", "Light Weapon Aptitude", "Lockpick Prowess",
   "Magic Item Tinkering Expertise", "Magic Resistance", "Mana Conversion Prowess",
   "Missile Weapon Aptitude", "Monster Attunement", "Person Attunement", "Recklessness Prowess",
   "Salvaging Aptitude", "Shield Aptitude", "Sneak Attack Prowess", "Sprint", "Summoning Prowess",
   "Two Handed Combat Aptitude", "Void Magic Aptitude", "War Magic Aptitude",
   "Weapon Tinkering Expertise",
}
-- Slots that can be locked: the name the desktop command knows, the label, and
-- how to recognise what is worn there (jewellery by equip slot, the rest by
-- the body coverage the client reports for the piece).
local LOCK_SLOTS = {
  { "trinket", "Trinket", slots = 0x4000000 }, { "necklace", "Necklace", slots = 0x8000 },
  { "bracelets", "Bracelets", slots = 0x30000 }, { "rings", "Rings", slots = 0xC0000 },
  { "head", "Head", cover = 0x4000 }, { "chest", "Chest", cover = 0x400 }, { "abdomen", "Abdomen", cover = 0x800 },
  { "upper-arms", "Upper arms", cover = 0x1000 }, { "lower-arms", "Lower arms", cover = 0x2000 },
  { "hands", "Hands", cover = 0x8000 }, { "upper-legs", "Upper legs", cover = 0x100 },
  { "lower-legs", "Lower legs", cover = 0x200 }, { "feet", "Feet", cover = 0x10000 },
  { "shirt", "Shirt", cover = 0x8 | 0x20 | 0x40 }, { "pants", "Pants", cover = 0x2 | 0x4 | 0x10 },
  { "cloak", "Cloak", slots = 0x8000000 },
}
local BODY_SLOTS = 0x7FFF        -- equip slots that mean "clothing or armour"
-- Skills an item's spells can need before they light up.
local ASSUME = { { 14, "Arcane Lore" }, { 6, "Melee Defense" }, { 7, "Missile Defense" }, { 15, "Magic Defense" } }
local SAVE_DELAY = 1.5           -- seconds of quiet before a change is written to disk

local cfg = nil                  -- this character's settings, once read (see new_cfg)
local cfg_tries, cfg_asked = 0, -100
local cfg_dirty = nil            -- when the last unsaved change was made
local cfg_note = ""
local point_ids = {}             -- control id -> cantrip name, from the last draw
local add_name, add_points = nil, 40

local function new_cfg()
  return { attack = "auto", points = {}, usable_only = false, fill_slots = false, ignore_held = false,
           lock_slots = {}, assume = {} }
end

local function attack_by(field, value)
  for _, a in ipairs(ATTACKS) do if a[field] == value then return a end end
  return nil
end

-- The attack skill to build for when the choice is "automatic": specialised
-- beats trained, then the higher base skill. Same rule as the desktop command.
local function guess_attack(s)
  local best, best_training, best_base = nil, 0, -1
  for _, a in ipairs(ATTACKS) do
    local sk = a.skill and type(s.skills) == "table" and s.skills[tostring(a.skill)] or nil
    if type(sk) == "table" then
      local training, base = tonumber(sk.training) or 0, tonumber(sk.base) or 0
      if training >= 2 and (training > best_training or (training == best_training and base > best_base)) then
        best, best_training, best_base = a, training, base
      end
    end
  end
  return best
end

-- Default points for a Major of each cantrip, given the attack skill, and the
-- order to list them in (highest first, then by name).
local function default_points(attack)
  local points = {}
  for _, name in ipairs(SUPPORT) do points[name] = SUPPORT_POINTS end
  if attack then for _, name in ipairs(attack.attrs) do points[name] = SUPPORT_POINTS end end
  for _, name in ipairs(WARDS) do points[name] = WARD_POINTS end
  for _, f in ipairs(FIXED) do points[f[1]] = f[2] end
  if attack then points[attack.cantrip] = ATTACK_POINTS end
  local order = {}
  for name in pairs(points) do order[#order + 1] = name end
  table.sort(order, function(a, b)
    if points[a] ~= points[b] then return points[a] > points[b] end
    return a < b
  end)
  return points, order
end

local function on_settings_result(s, e, now)
  local d = e.data
  if not (e.ok and type(d) == "table" and d.kind == "cantrip_tailor_settings") then
    -- No file yet is the normal first-run case. But a read can also fail for
    -- a passing reason, and treating that as "no settings" would let the next
    -- change overwrite a good file - so ask once more before believing it.
    if cfg_tries < 2 then cfg_asked = now - 3; return end
    cfg = new_cfg()
    return
  end
  local c = new_cfg()
  if attack_by("key", d.attack) then c.attack = d.attack end
  if type(d.points) == "table" then
    for name, pts in pairs(d.points) do
      if type(name) == "string" and type(pts) == "number" and pts >= 0 then c.points[name] = pts end
    end
  end
  c.usable_only, c.fill_slots, c.ignore_held = d.usable_only == true, d.fill_slots == true, d.ignore_held == true
  if type(d.lock_slots) == "table" then
    for _, key in ipairs(d.lock_slots) do c.lock_slots[tostring(key)] = true end
  end
  if type(d.assume) == "table" then
    for name, value in pairs(d.assume) do
      if type(name) == "string" and type(value) == "number" and value > 0 then c.assume[name] = value end
    end
  end
  cfg = c
end

local function settings_file(s)
  local locks = {}
  for _, slot in ipairs(LOCK_SLOTS) do
    if cfg.lock_slots[slot[1]] then locks[#locks + 1] = slot[1] end
  end
  return {
    kind = "cantrip_tailor_settings", plugin = "cantrip_tailor", plugin_version = VERSION,
    character = tostring(s.character_name or ""), server = tostring(s.server or ""),
    unix_time = math.floor(tonumber(s.unix_time) or 0),
    attack = cfg.attack, points = cfg.points, usable_only = cfg.usable_only, fill_slots = cfg.fill_slots,
    ignore_held = cfg.ignore_held, lock_slots = locks, assume = cfg.assume,
  }
end

-- One change from the Settings tab. Returns true if it was one of ours.
local function on_settings_change(s, e)
  if not cfg then return false end
  local id, value = tostring(e.id or ""), e.value
  if e.type == "click" then
    if id == "reset" then cfg.points = {}
    elseif id == "add_go" then
      if add_name and (tonumber(add_points) or 0) > 0 then cfg.points[add_name] = tonumber(add_points) end
      add_name = nil
    else return false end
  elseif id == "attack" then
    local a = attack_by("label", value)
    cfg.attack = a and a.key or "auto"
  elseif id == "usable" then cfg.usable_only = value == true
  elseif id == "fill" then cfg.fill_slots = value == true
  elseif id == "held" then cfg.ignore_held = value ~= true
  elseif id == "add_name" then add_name = tostring(value); return true        -- not a setting yet
  elseif id == "add_pts" then add_points = tonumber(value) or 0; return true
  elseif point_ids[id] then
    local name, pts = point_ids[id], math.max(0, tonumber(value) or 0)
    local attack = attack_by("key", cfg.attack) or guess_attack(s)
    local defaults = default_points(attack)
    if defaults[name] == pts then cfg.points[name] = nil            -- back to the default: nothing to remember
    elseif defaults[name] == nil and pts == 0 then cfg.points[name] = nil   -- an added cantrip set to 0 just goes away
    else cfg.points[name] = pts end
  elseif string.sub(id, 1, 3) == "lk_" then
    cfg.lock_slots[string.sub(id, 4)] = (value == true) or nil
  elseif string.sub(id, 1, 3) == "as_" then
    for _, a in ipairs(ASSUME) do
      if tostring(a[1]) == string.sub(id, 4) then
        local v = tonumber(value) or 0
        cfg.assume[a[2]] = (v > 0) and v or nil
      end
    end
  else return false end
  cfg_dirty = tonumber(s.time) or 0
  cfg_note = "Not saved yet..."
  return true
end

local function settings_rows(s, inventory)
  if not cfg then return { { id = "s_wait", text = "Reading this character's settings..." } } end
  local rows = {
    { id = "s_intro", text = "Saved for this character. After changing anything, run cantrip-tailor again to re-plan." },
  }
  if cfg_note ~= "" then rows[#rows + 1] = { id = "s_note", text = cfg_note, color = cfg_dirty and "yellow" or "green" } end

  -- Attack skill and the points table.
  local chosen = attack_by("key", cfg.attack)
  local guessed = guess_attack(s)
  local attack = chosen or guessed
  local options = { AUTO }
  for _, a in ipairs(ATTACKS) do options[#options + 1] = a.label end
  local pri = {
    { id = "attack", type = "select", text = "Attack skill to build for", value = chosen and chosen.label or AUTO, options = options },
  }
  if not chosen then
    pri[#pri + 1] = { id = "s_guess", color = guessed and "green" or "red",
      text = guessed and ("Automatic picks " .. guessed.label .. " for this character.")
                      or "Can't tell your attack skill from your skills - please pick one." }
  end
  pri[#pri + 1] = { id = "s_pts", text = "Points for a Major of each cantrip (a Minor counts a third, a ward's Minor two thirds). 0 = ignore it." }
  local defaults, order = default_points(attack)
  point_ids = {}
  local function point_row(name, default)
    local id = "pt_" .. slug(name)
    point_ids[id] = name
    local value = cfg.points[name]
    local label = name
    if value ~= nil and default ~= nil then label = name .. "  (default " .. string.format("%.0f", default) .. ")" end
    if default == nil then label = name .. "  (added)" end
    pri[#pri + 1] = { id = id, type = "number", text = label, min = 0, max = 300, step = 5, value = value or default }
  end
  local added = {}
  for name in pairs(cfg.points) do if defaults[name] == nil then added[#added + 1] = name end end
  table.sort(added)
  for _, name in ipairs(added) do point_row(name, nil) end          -- what you added, first
  for _, name in ipairs(order) do point_row(name, defaults[name]) end
  local others = {}
  for _, list in ipairs({ KNOWN, WARDS }) do
    for _, name in ipairs(list) do
      if defaults[name] == nil and cfg.points[name] == nil then others[#others + 1] = name end
    end
  end
  table.sort(others)
  if #others > 0 then
    local found = false
    for _, name in ipairs(others) do if name == add_name then found = true end end
    if not found then add_name = others[1] end
    pri[#pri + 1] = { id = "add_row", type = "row", children = {
      { id = "add_name", type = "select", text = "Add a cantrip", value = add_name, options = others },
      { id = "add_pts", type = "number", text = "Points", min = 5, max = 300, step = 5, value = add_points },
      { id = "add_go", type = "button", text = "Add", width = 90 },
    } }
  end
  pri[#pri + 1] = { id = "reset", type = "button", text = "Reset every score to its default" }
  rows[#rows + 1] = { id = "sec_pri", type = "section", text = "What to value", expanded = true, children = pri }

  rows[#rows + 1] = { id = "sec_rules", type = "section", text = "Rules for the set", expanded = true, children = {
    { id = "usable", type = "checkbox", value = cfg.usable_only,
      text = "Only pieces that work right now (I can wield them and their spells would activate)" },
    { id = "fill", type = "checkbox", value = cfg.fill_slots,
      text = "Fill every slot (best armour where no cantrip matters)" },
    { id = "held", type = "checkbox", value = not cfg.ignore_held,
      text = "Count the cantrips on what's in my hands" },
  } }

  -- Locks, labelled with what is being worn there right now.
  local worn = {}
  for _, item in ipairs(inventory) do
    if item.equipped then
      local ints = item.int_properties or {}
      local cover = tonumber(ints["4"]) or tonumber(ints["218103821"]) or 0
      for _, slot in ipairs(LOCK_SLOTS) do
        local here = (slot.slots and band(item.slots, slot.slots) ~= 0)
          or (slot.cover and band(item.slots, BODY_SLOTS) ~= 0 and band(cover, slot.cover) ~= 0)
        if here then worn[slot[1]] = (worn[slot[1]] and (worn[slot[1]] .. ", ") or "") .. tostring(item.name or "?") end
      end
    end
  end
  local locks = { { id = "s_lock", text = "Ticked slots keep whatever you are wearing there when the plan is made." } }
  for _, slot in ipairs(LOCK_SLOTS) do
    locks[#locks + 1] = { id = "lk_" .. slot[1], type = "checkbox", value = cfg.lock_slots[slot[1]] == true,
      text = short(slot[2] .. ":  " .. (worn[slot[1]] or "(nothing worn)"), 200) }
  end
  rows[#rows + 1] = { id = "sec_lock", type = "section", text = "Keep what I'm wearing", expanded = false, children = locks }

  local assume = { { id = "s_assume", text = "If you buff a skill up before dressing, say where it will be. 0 = use the skill as it was at the dump." } }
  for _, a in ipairs(ASSUME) do
    local sk = type(s.skills) == "table" and s.skills[tostring(a[1])] or nil
    local has = type(sk) == "table" and tonumber(sk.current) or nil
    assume[#assume + 1] = { id = "as_" .. a[1], type = "number", min = 0, max = 1000, step = 5, value = cfg.assume[a[2]] or 0,
      text = a[2] .. (has and ("  (now " .. string.format("%.0f", has) .. ")") or "") }
  end
  rows[#rows + 1] = { id = "sec_assume", type = "section", text = "Skills I'll buff to", expanded = false, children = assume }
  return rows
end

local function enqueue(id, name, data)
  queue[#queue + 1] = { id = id, service = "profiles", operation = "save", group = "Dumps", name = name, data = data }
  files_expected = files_expected + 1
end

local function begin_write(s, gear)
  local ids = {}
  for _, item in ipairs(gear) do ids[#ids + 1] = item.id end
  job = {
    phase = "write", ids = ids, pos = 1, chunk = 0,
    chunks = math.max(1, math.ceil(#ids / CHUNK)),
    dump_id = math.floor(tonumber(s.unix_time) or 0),
    slug = slug(tostring(s.character_name or "")),
    written = 0, unidentified = 0,
  }
  files_ok, files_failed, files_expected = 0, 0, 0
  last_error = ""
end

local function meta(s, extra)
  local m = {
    plugin = "cantrip_tailor", plugin_version = VERSION, dump_id = job.dump_id,
    character = tostring(s.character_name or ""), level = s.level or 0,
    player = idkey(s.player), server = tostring(s.server or ""),
    unix_time = math.floor(tonumber(s.unix_time) or 0),
  }
  for k, v in pairs(extra) do m[k] = v end
  return m
end

-- One unit of writing per callback: a file of items, then the index.
local function step_write(s, by_id, inventory)
  if job.pos <= #job.ids then
    job.chunk = job.chunk + 1
    local items = {}
    local last = math.min(#job.ids, job.pos + CHUNK - 1)
    for i = job.pos, last do
      local item = by_id[idkey(job.ids[i])]
      if item then               -- it may have been dropped or sold since the scan
        items[#items + 1] = clean(item, 5)
        job.written = job.written + 1
        if not item.identified then job.unidentified = job.unidentified + 1 end
      end
    end
    job.pos = last + 1
    enqueue("gear_" .. job.chunk, "gear_" .. job.slug .. "_" .. job.chunk,
      meta(s, { kind = "gear", chunk = job.chunk, chunks = job.chunks, items = items }))
    status = "Writing file " .. job.chunk .. " of " .. job.chunks .. "..."
    return
  end
  -- Packs, so the reader can say which bag a piece is in. Inventory order is
  -- kept: it is the order the packs appear down the side of the panel.
  local containers = {}
  for _, item in ipairs(inventory) do
    if band(item.type, CONTAINER) ~= 0 then
      containers[#containers + 1] = { id = item.id, name = tostring(item.name or "") }
    end
  end
  enqueue("index", "gear_" .. job.slug, meta(s, {
    kind = "gear_index", chunks = job.chunks, items = job.written, unidentified = job.unidentified,
    containers = containers, skills = clean(s.skills, 3) or {},
  }))
  last_summary = job.written .. " item(s), " .. job.unidentified .. " without spell data"
  status = "Dump queued (" .. last_summary .. "). Waiting for the files to be written..."
  job = nil
end

return function(s, p)
  local inventory = s.inventory or {}
  local now = tonumber(s.time) or 0

  -- 1. Events: buttons, chat commands, and the results of our file requests.
  local want_scan, want_dump = false, false
  for _, e in ipairs(s.events or {}) do
    if e.type == "click" and e.id == "scan" then want_scan = true
    elseif e.type == "click" and e.id == "dump" then want_dump = true
    elseif e.type == "click" and e.id == "cancel" then
      if job then job = nil; status = "Cancelled." end
    elseif e.type == "click" and e.id == "reload" then
      plan_asked = -100
    elseif (e.type == "change" or e.type == "click") and on_settings_change(s, e) then
      -- handled: a Settings tab control
    elseif e.type == "click" and (e.id == "next" or string.sub(tostring(e.id or ""), 1, 2) == "wb") then
      click_note = (e.ok == false) and ("Couldn't select that piece: " .. tostring(e.error or "unknown reason")) or ""
    elseif e.type == "command" then
      -- /acueplugin cantrip_tailor scan | dump | plan
      if e.text == "dump" then want_dump = true
      elseif e.text == "plan" then plan_asked = -100
      else want_scan = true end
    elseif e.type == "result" and e.id == "plan" then
      on_plan_result(s, e)
    elseif e.type == "result" and e.id == "settings" then
      on_settings_result(s, e, now)
    elseif e.type == "result" and e.id == "settings_save" then
      cfg_note = e.ok and "Saved. Run cantrip-tailor again to re-plan."
        or ("Couldn't save: " .. tostring(e.error or "unknown error"))
    elseif e.type == "result" then
      if e.ok then files_ok = files_ok + 1
      else
        files_failed = files_failed + 1
        last_error = tostring(e.id or "?") .. ": " .. tostring(e.error or "unknown error")
      end
      if not job and files_expected > 0 and files_ok + files_failed >= files_expected then
        status = (files_failed == 0)
          and ("Dump complete: " .. last_summary .. " in " .. files_ok .. " file(s).")
          or ("Dump finished with " .. files_failed .. " failed file(s). See the error below.")
      end
    end
  end

  -- 2. Census of what we'd dump.
  local gear, by_id, unknown, worn = {}, {}, 0, 0
  for _, item in ipairs(inventory) do
    by_id[idkey(item.id)] = item
    if is_gear(item) then
      gear[#gear + 1] = item
      if not item.identified then unknown = unknown + 1 end
      if item.equipped then worn = worn + 1 end
    end
  end

  if not job and #gear > 0 then
    if want_scan then
      job = { phase = "identify" }
      tries = {}
    elseif want_dump then
      begin_write(s, gear)
    end
  end

  -- 3. Appraise what we haven't seen, one request at a time.
  local out = {}
  if job and job.phase == "identify" then
    local pending, target = 0, nil
    for _, item in ipairs(gear) do
      if not item.identified then
        local t = tries[idkey(item.id)]
        if not t or t.n < MAX_TRIES or now - t.at < RETRY_AFTER then
          pending = pending + 1
          if not target and (not t or (t.n < MAX_TRIES and now - t.at >= RETRY_AFTER)) then target = item end
        end
      end
    end
    if pending == 0 then
      begin_write(s, gear)
    else
      status = "Appraising: " .. pending .. " to go..."
      if target and now - last_identify >= IDENTIFY_GAP then
        local key = idkey(target.id)
        local t = tries[key] or { n = 0, at = now }
        t.n, t.at = t.n + 1, now
        tries[key] = t
        last_identify = now
        out.action, out.target = "identify", target.id
        status = "Appraising " .. tostring(target.name or "?") .. " (" .. pending .. " to go)..."
      end
    end
  end

  -- 4. Write one file's worth; look for a (newer) plan now and then; hand
  --    over up to four queued requests.
  if job and job.phase == "write" then step_write(s, by_id, inventory) end
  local named = (s.character_name or "") ~= ""
  if not job and #queue == 0 and named and now - plan_asked >= PLAN_POLL then
    ask_for_plan(s, now)
  end
  -- Settings: read them once at the start; write them once a change has settled.
  local settings_name = "settings_" .. slug(tostring(s.character_name or ""))
  if not cfg and named and now - cfg_asked >= 5 then
    cfg_tries, cfg_asked = cfg_tries + 1, now
    queue[#queue + 1] = { id = "settings", service = "profiles", operation = "read", group = "Settings", name = settings_name }
  elseif cfg and cfg_dirty and now - cfg_dirty >= SAVE_DELAY then
    cfg_dirty = nil
    queue[#queue + 1] = { id = "settings_save", service = "profiles", operation = "save", group = "Settings",
                          name = settings_name, data = settings_file(s) }
  end
  local requests = {}
  for _ = 1, math.min(4, #queue) do requests[#requests + 1] = table.remove(queue, 1) end

  -- 5. UI: three tabs. Plan first, because that is the one you come back to.
  local idle = (job == nil) and #gear > 0
  local dump_tab = {
    { id = "who", text = tostring(s.character_name or "?") .. "  (level " .. string.format("%.0f", tonumber(s.level) or 0) .. ")  on " .. tostring(s.server or "?") },
    { id = "counts", text = "Wearing or wielding " .. worn .. "   |   gear in total: " .. #gear .. "   |   not appraised yet: " .. unknown },
    { id = "status", text = status, color = (files_failed > 0) and "red" or "green" },
    { id = "scan", type = "button", text = "Scan + dump  (appraises " .. unknown .. ", about " .. math.ceil(unknown * IDENTIFY_GAP) .. " s)", enabled = idle },
    { id = "dump", type = "button", text = "Dump now  (no appraisals)", enabled = idle },
  }
  if job then dump_tab[#dump_tab + 1] = { id = "cancel", type = "button", text = "Cancel" } end
  dump_tab[#dump_tab + 1] = { id = "progress", text = "Files written: " .. files_ok .. " / " .. files_expected }
  dump_tab[#dump_tab + 1] = { id = "help", text = "Then, on your desktop, in the Cantrip Tailor folder:  uv run cantrip-tailor" }
  if last_error ~= "" then dump_tab[#dump_tab + 1] = { id = "error", text = "Last error: " .. last_error, color = "red" } end
  if (s.error or "") ~= "" then dump_tab[#dump_tab + 1] = { id = "client", text = "Client: " .. tostring(s.error), color = "red" } end
  local ui = {
    { id = "tabs", type = "tabs", children = {
      { id = "tab_plan", type = "column", text = "Plan", children = plan_rows(s, by_id, inventory) },
      { id = "tab_dump", type = "column", text = "Dump", children = dump_tab },
      { id = "tab_settings", type = "column", text = "Settings", children = settings_rows(s, inventory) },
    } },
  }

  out.ui, out.requests, out.status = ui, requests, status
  return out
end
