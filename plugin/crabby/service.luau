--!nonstrict
-- Crabby service - owns all talk to the crabby bridge.
--
-- Mirrors bridge snapshots (SSE /events, one JSON per "data:" line) into
-- noctalia.state "crabby.view"; bar and panel only render it. The stream is
-- held only while it is useful (panel open, session or voice active, or a
-- command was just sent), so an idle bridge can exit and claude never runs
-- in the background for nothing. Commands arrive on "crabby.cmd" as
-- "<nonce>|<cmd>|<payload>" or over IPC.

noctalia.setUpdateInterval(1000)

local base = ""
local stream = nil
local streamGen = 0 -- callbacks of a replaced stream must not touch the current one
local view = nil
local gotData = false
local panelOpen = false
local wantUntil = 0 -- os.time() until which a recent command keeps the stream
local retryAt = 0

local sendClient -- defined after base/post helpers; onLine needs it first

local OFF_VIEW = { active = false, status = "off", voice = "idle", items = {}, permissions = {} }

local function readConfig()
  local port = math.clamp(tonumber(noctalia.getConfig("api_port")) or 18766, 1, 65535)
  base = `http://127.0.0.1:{port}`
end
readConfig()

local function publish(raw)
  noctalia.state.set("crabby.view", raw)
end

local function publishTable(t)
  publish((noctalia.json.encode(t)))
end

local function wanted()
  if panelOpen or os.time() < wantUntil then
    return true
  end
  return view ~= nil and (view.active == true or (view.voice or "idle") ~= "idle")
end

local function disconnect()
  if stream then
    streamGen += 1
    stream.stop()
    stream = nil
  end
end

local function onLine(gen, line)
  if gen ~= streamGen then
    return
  end
  if string.sub(line, 1, 6) ~= "data: " then
    return
  end
  local raw = string.sub(line, 7)
  local decoded = noctalia.json.decode(raw)
  if type(decoded) ~= "table" then
    return
  end
  if not gotData then
    sendClient()
  end
  gotData = true
  view = decoded
  publish(raw)
end

local function onClose(gen)
  if gen ~= streamGen then
    return
  end
  stream = nil
  if not gotData and wanted() then
    publishTable({ offline = true, active = false, status = "off", voice = "idle", items = {}, permissions = {} })
  end
  retryAt = os.time() + 2
end

local function connect()
  if stream then
    return
  end
  gotData = false
  streamGen += 1
  local gen = streamGen
  stream = noctalia.httpStream(
    { url = base .. "/events", headers = { "Accept: text/event-stream" } },
    function(line)
      onLine(gen, line)
    end,
    function()
      onClose(gen)
    end
  )
  if stream == nil then
    retryAt = os.time() + 3
  end
end

-- Tells the bridge whether the panel is open (it notifies only when closed)
-- and hands it notification texts in the shell's current language.
function sendClient()
  noctalia.http({
    url = base .. "/client",
    method = "POST",
    body = (noctalia.json.encode({
      panel_open = panelOpen,
      strings = {
        notify_done = noctalia.tr("notify.done"),
        notify_done_body = noctalia.tr("notify.doneBody"),
        notify_permission = noctalia.tr("notify.permission"),
        action_allow = noctalia.tr("action.allow"),
        action_open = noctalia.tr("notify.open"),
      },
    })),
    headers = { "Content-Type: application/json" },
  }, function(_res) end)
end

local function post(path, body)
  wantUntil = os.time() + 15
  connect()
  noctalia.http({
    url = base .. path,
    method = "POST",
    body = (noctalia.json.encode(body or {})),
    headers = { "Content-Type: application/json" },
  }, function(res)
    if not res.ok or res.status ~= 200 then
      noctalia.notifyError(noctalia.tr("title"), noctalia.tr("toast.bridgeDown"))
    end
  end)
end

local ROUTES = {
  interrupt = "/interrupt",
  start = "/session/start",
  stop = "/session/stop",
  new = "/session/new",
  terminal = "/terminal",
  voice = "/voice/toggle",
  voice_cancel = "/voice/cancel",
}

local function dispatch(cmd, payload)
  payload = payload or ""
  if ROUTES[cmd] then
    post(ROUTES[cmd])
  elseif cmd == "send" then
    if noctalia.string.trim(payload) ~= "" then
      post("/send", { text = payload })
    end
  elseif cmd == "permission" then
    local id, allow = string.match(payload, "^(.*)|([01])$")
    if id then
      post("/permission", { id = id, allow = allow == "1" })
    end
  elseif cmd == "model" then
    post("/model", { model = payload })
  elseif cmd == "allow_all" then
    post("/allow_all", { value = payload == "true" })
  elseif cmd == "refresh" then
    connect()
  elseif cmd == "debug" then
    -- Noctalia's log goes nowhere by default; dump internals to a file instead.
    noctalia.writeFile(noctalia.expandPath("~/.local/state/crabby/service-debug.json"), (noctalia.json.encode({
      connected = stream ~= nil,
      streamGen = streamGen,
      gotData = gotData,
      panelOpen = panelOpen,
      wantUntil = wantUntil,
      now = os.time(),
      wanted = wanted(),
      status = view and view.status,
      active = view and view.active,
      voice = view and view.voice,
    }, true)))
  else
    noctalia.log(`crabby: unknown command {cmd}`)
  end
end

noctalia.state.watch("crabby.cmd", function(value)
  if type(value) ~= "string" then
    return
  end
  local _nonce, cmd, payload = string.match(value, "^([^|]*)|([^|]*)|(.*)$")
  if cmd then
    dispatch(cmd, payload)
  end
end)

noctalia.state.watch("crabby.panel", function(value)
  local open = type(value) == "string" and string.sub(value, 1, 4) == "open"
  if open == panelOpen then
    return
  end
  panelOpen = open
  if open then
    connect()
  end
  if stream then
    sendClient()
  end
end)

function onIpc(event, payload)
  dispatch(event, payload)
end

function onConfigChanged()
  readConfig()
  disconnect()
end

function update()
  if wanted() then
    if stream == nil and os.time() >= retryAt then
      connect()
    end
  elseif stream then
    disconnect()
  end
end

publishTable(OFF_VIEW)

-- Reattach to a bridge that already runs (shell restart mid-session) without
-- waking a socket-activated one that is asleep.
noctalia.runAsync({ "systemctl", "--user", "is-active", "crabby-bridge.service" }, function(result)
  if noctalia.string.trim(result.stdout or "") == "active" then
    wantUntil = os.time() + 5
    connect()
  end
end)
