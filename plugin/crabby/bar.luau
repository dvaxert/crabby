--!nonstrict
-- Crabby bar widget: asterisk glyph colored by session state (pulses while
-- Claude works); click opens the panel, right click toggles voice input.

local common = require("./lib/common.luau")

local view = nil
local phase = false

local function color()
  if view == nil or view.offline then
    return "on_surface_variant/0.5"
  end
  if view.voice == "recording" then
    return phase and "error" or "error/0.5"
  elseif view.voice == "transcribing" then
    return "tertiary"
  end
  local s = view.status
  if s == "permission" then
    return phase and "error" or "error/0.45"
  elseif s == "thinking" or s == "tool" or s == "starting" then
    return phase and "primary" or "primary/0.45"
  elseif s == "idle" then
    return "primary"
  end
  return "on_surface_variant"
end

local function animated()
  return view ~= nil and (common.busy(view) or view.voice == "recording")
end

local function render()
  noctalia.setUpdateInterval(animated() and 600 or 60000)
  barWidget.render(ui.row({ align = "center" }, {
    ui.glyph({ name = "asterisk", color = color() }),
  }))
  barWidget.setTooltip({
    { noctalia.tr("title"), common.statusLine(view) },
  })
end

noctalia.state.watch("crabby.view", function(value)
  view = common.decode(value) or view
  render()
end)

function update()
  phase = not phase
  render()
end

function onClick()
  noctalia.togglePanel(common.PANEL)
end

function onRightClick()
  common.send("voice")
end
