--!nonstrict
-- Helpers shared by the bar and panel entries (each entry gets its own copy).

local M = {}

M.PANEL = "dvaxert/crabby:panel"
M.MODELS = { "haiku", "sonnet", "opus" }

function M.send(cmd, payload)
  noctalia.state.set("crabby.cmd", `{noctalia.nowMs()}|{cmd}|{payload or ""}`)
end

function M.decode(value)
  if type(value) ~= "string" then
    return nil
  end
  local view = noctalia.json.decode(value)
  return type(view) == "table" and view or nil
end

function M.busy(view)
  local s = view and view.status or "off"
  return s == "thinking" or s == "tool" or s == "permission" or s == "starting"
end

local function turnSuffix(view)
  local turn = view.turn
  if type(turn) ~= "table" then
    return ""
  end
  local secs = math.max(0, math.floor((noctalia.nowMs() - (turn.started or 0)) / 1000))
  local tokens = turn.tokens or 0
  if tokens > 0 then
    return noctalia.tr("status.turnTokens", { secs = secs, tokens = tokens })
  end
  return noctalia.tr("status.turn", { secs = secs })
end

-- One line, Claude Code style: "✻ Думаю… (12s · ↓ 340 tok)".
function M.statusLine(view)
  if view == nil or view.offline then
    return noctalia.tr("status.offline")
  end
  if view.voice == "recording" then
    return noctalia.tr("status.recording")
  elseif view.voice == "transcribing" then
    return noctalia.tr("status.transcribing")
  end
  local s = view.status
  if s == "thinking" then
    return noctalia.tr("status.thinking") .. turnSuffix(view)
  elseif s == "tool" then
    local tool = view.turn and view.turn.tool or ""
    return `⚙ {tool ~= "" and tool or noctalia.tr("status.tool")}` .. turnSuffix(view)
  elseif s == "permission" then
    return noctalia.tr("status.permission")
  elseif s == "starting" then
    return noctalia.tr("status.starting")
  elseif s == "idle" then
    return noctalia.tr("status.idle", { model = view.model or "" })
  elseif s == "terminal" then
    return noctalia.tr("status.terminal")
  end
  return noctalia.tr("status.off")
end

return M
