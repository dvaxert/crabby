--!nonstrict
-- Crabby panel: chat with the panel's Claude session.
--
-- header: model switch, "to terminal", new / end session
-- body:   messages, collapsible tool calls, permission cards
-- footer: Claude Code style status line + composer with mic and send

local common = require("./lib/common.luau")

local view = nil
local draft = ""
local inputRev = 0 -- bumping it recreates the composer: clears it and grabs focus
local scrollRev = 0
local expanded = {} -- tool_use id -> true
local lastItemCount = 0

local function send(cmd, payload)
  common.send(cmd, payload)
end

local function modelButton(model, current)
  return ui.button({
    key = "model-" .. model,
    text = noctalia.tr("model." .. model),
    controlSize = "sm",
    variant = model == current and "primary" or "ghost",
    tooltip = noctalia.tr("tooltip.model"),
    onClick = function()
      send("model", model)
    end,
  })
end

local function header()
  local active = view ~= nil and view.active == true
  local current = view and view.model or "haiku"
  local models = {}
  for _, m in ipairs(common.MODELS) do
    table.insert(models, modelButton(m, current))
  end
  return ui.column({ gap = 6 }, {
    ui.row({ gap = 4, align = "center" }, {
      ui.glyph({ name = "asterisk", size = 18, color = "primary" }),
      ui.label({ text = noctalia.tr("title"), fontSize = 15, fontWeight = "bold", flexGrow = 1 }),
      ui.button({
        glyph = "terminal-2",
        variant = "ghost",
        controlSize = "sm",
        tooltip = noctalia.tr("tooltip.terminal"),
        enabled = active and (view.session_id or "") ~= "",
        onClick = function()
          send("terminal")
        end,
      }),
      ui.button({
        glyph = "plus",
        variant = "ghost",
        controlSize = "sm",
        tooltip = noctalia.tr("tooltip.new"),
        onClick = function()
          send("new")
        end,
      }),
      ui.button({
        glyph = "power",
        variant = "ghost",
        controlSize = "sm",
        tooltip = noctalia.tr("tooltip.stop"),
        enabled = active,
        onClick = function()
          send("stop")
        end,
      }),
    }),
    ui.row({ gap = 4, align = "center" }, {
      ui.row({ gap = 2, align = "center", flexGrow = 1 }, models),
      ui.label({ text = noctalia.tr("panel.allowAll"), fontSize = 12, color = "on_surface_variant" }),
      ui.toggle({
        checked = view ~= nil and view.allow_all == true,
        onChange = function(value)
          send("allow_all", value)
        end,
      }),
    }),
  })
end

local function userItem(item)
  local children = {}
  if item.voice then
    table.insert(children, ui.glyph({ name = "microphone", size = 12, color = "on_surface_variant" }))
  end
  table.insert(children, ui.label({ text = item.text, maxWidth = 340 }))
  return ui.row({ justify = "end" }, {
    ui.row({ gap = 6, align = "center", fill = "primary/0.16", radius = 12, paddingH = 10, paddingV = 6 }, children),
  })
end

local TOOL_GLYPH = { running = "loader-2", done = "check", error = "x" }

local function toolItem(item)
  local id = item.id or ""
  local open = expanded[id] == true
  local children = {
    ui.row({ gap = 6, align = "center" }, {
      ui.glyph({
        name = TOOL_GLYPH[item.state] or "tool",
        size = 13,
        color = item.state == "error" and "error" or "on_surface_variant",
      }),
      ui.label({
        text = `{item.name}: {item.summary or ""}`,
        fontSize = 12,
        color = "on_surface_variant",
        maxLines = 1,
        flexGrow = 1,
      }),
      ui.glyph({ name = open and "chevron-up" or "chevron-down", size = 12, color = "on_surface_variant" }),
    }),
  }
  if open and (item.output or "") ~= "" then
    table.insert(children, ui.label({
      text = item.output,
      fontSize = 11,
      fontFamily = "monospace",
      color = "on_surface_variant",
      maxLines = 30,
    }))
  end
  return ui.column({
    key = "tool-" .. id,
    gap = 4,
    fill = "surface_variant/0.5",
    radius = 8,
    paddingH = 8,
    paddingV = 5,
    onClick = function()
      expanded[id] = not open or nil
      render()
    end,
  }, children)
end

local function systemItem(item)
  return ui.label({
    text = item.text,
    fontSize = 12,
    color = item.error and "error" or "on_surface_variant",
    textAlign = "center",
    maxLines = 4,
  })
end

local function permissionCard(p)
  return ui.column({
    key = "perm-" .. p.id,
    gap = 8,
    fill = "error/0.10",
    border = "error/0.6",
    borderWidth = 1,
    radius = 10,
    padding = 10,
  }, {
    ui.label({ text = noctalia.tr("panel.permission", { tool = p.tool }), fontWeight = "bold" }),
    ui.label({ text = p.description or "", fontSize = 12, color = "on_surface_variant", visible = (p.description or "") ~= "" }),
    ui.label({ text = p.summary or "", fontSize = 12, fontFamily = "monospace", maxLines = 6 }),
    ui.row({ gap = 6, justify = "end" }, {
      ui.button({
        text = noctalia.tr("action.deny"),
        variant = "outline",
        controlSize = "sm",
        onClick = function()
          send("permission", p.id .. "|0")
        end,
      }),
      ui.button({
        text = noctalia.tr("action.allow"),
        variant = "primary",
        controlSize = "sm",
        onClick = function()
          send("permission", p.id .. "|1")
        end,
      }),
    }),
  })
end

local function body()
  local rows = {}
  local items = view and view.items or {}
  for i, item in ipairs(items) do
    if item.k == "user" then
      table.insert(rows, userItem(item))
    elseif item.k == "assistant" then
      if (item.text or "") ~= "" then
        table.insert(rows, ui.markdown({ key = `md-{i}`, text = item.text }))
      end
    elseif item.k == "tool" then
      table.insert(rows, toolItem(item))
    elseif item.k == "system" then
      table.insert(rows, systemItem(item))
    end
  end
  for _, p in ipairs(view and view.permissions or {}) do
    table.insert(rows, permissionCard(p))
  end
  if #rows == 0 then
    table.insert(rows, ui.column({ gap = 6, align = "center", paddingV = 40 }, {
      ui.glyph({ name = "asterisk", size = 32, color = "primary/0.5" }),
      ui.label({ text = noctalia.tr("panel.empty"), color = "on_surface_variant", textAlign = "center" }),
      ui.label({ text = noctalia.tr("panel.emptyHint"), fontSize = 12, color = "on_surface_variant/0.7" }),
    }))
  end
  return ui.scroll({ flexGrow = 1, gap = 10, stickToBottom = true, scrollToBottomRev = scrollRev }, rows)
end

local function composerButtons(recording)
  local buttons = {}
  if recording then
    table.insert(buttons, ui.button({
      glyph = "x",
      variant = "ghost",
      controlSize = "sm",
      tooltip = noctalia.tr("tooltip.voiceCancel"),
      onClick = function()
        send("voice_cancel")
      end,
    }))
  end
  table.insert(buttons, ui.button({
    glyph = recording and "microphone-filled" or "microphone",
    variant = recording and "destructive" or "ghost",
    controlSize = "sm",
    tooltip = noctalia.tr("tooltip.voice"),
    onClick = function()
      send("voice")
    end,
  }))
  table.insert(buttons, ui.button({
    glyph = "send",
    variant = "primary",
    controlSize = "sm",
    tooltip = noctalia.tr("tooltip.send"),
    onClick = function()
      onDraftSubmit(draft)
    end,
  }))
  return buttons
end

local function footer()
  local busy = common.busy(view)
  local recording = view ~= nil and view.voice == "recording"
  local status = { ui.label({
    text = common.statusLine(view),
    fontSize = 12,
    color = view and view.status == "permission" and "error" or "on_surface_variant",
    maxLines = 1,
    flexGrow = 1,
  }) }
  if busy then
    table.insert(status, ui.button({
      text = noctalia.tr("action.interrupt"),
      glyph = "player-stop",
      variant = "ghost",
      controlSize = "sm",
      onClick = function()
        send("interrupt")
      end,
    }))
  end
  return ui.column({ gap = 6 }, {
    ui.row({ gap = 6, align = "center", height = 28 }, status),
    ui.row({ gap = 6, align = "end" }, {
      ui.input({
        key = `composer-{inputRev}`,
        value = draft,
        placeholder = noctalia.tr("panel.placeholder"),
        multiline = true,
        submitOnEnter = true,
        focus = true,
        height = 64,
        flexGrow = 1,
        onChange = "onDraftChange",
        onSubmit = "onDraftSubmit",
      }),
      ui.column({ gap = 4 }, composerButtons(recording)),
    }),
  })
end

function render()
  panel.render(ui.column({ flexGrow = 1, gap = 8 }, {
    header(),
    ui.separator({}),
    body(),
    footer(),
  }))
end

local function applyView(value)
  view = common.decode(value) or view
  local count = view and view.items and #view.items or 0
  if count < lastItemCount then
    expanded = {} -- new session
  end
  lastItemCount = count
end

noctalia.state.watch("crabby.view", function(value)
  applyView(value)
  render()
end)

function onOpen(_context)
  -- Watches are not delivered while the panel is closed: catch up first.
  applyView(noctalia.state.get("crabby.view"))
  noctalia.state.set("crabby.panel", `open|{noctalia.nowMs()}`)
  panel.setWantsSecondTicks(true)
  scrollRev += 1
  render()
end

function onClose()
  noctalia.state.set("crabby.panel", `closed|{noctalia.nowMs()}`)
end

-- Second ticks keep the elapsed time in the status line moving.
function update()
  if common.busy(view) then
    render()
  end
end

function onDraftChange(value)
  draft = value
end

function onDraftSubmit(value)
  local text = noctalia.string.trim(value or "")
  if text == "" then
    return
  end
  send("send", text)
  draft = ""
  inputRev += 1
  scrollRev += 1
  render()
end
