local opened = false
local hours = 24
local anchor = nil
local calendar = false
local days = {}
local dateIndex = 1
local hover = nil
local requestPath = nil
local socketPath = nil
local status = noctalia.state.get("status") or {}

local function now() return math.floor(noctalia.nowMs() / 1000) end
local function quote(value) return "'" .. value:gsub("'", "'\\''") .. "'" end

local function sendRequest()
    if not requestPath then return end
    local colors = {}
    for _, role in ipairs({"primary", "surface_container", "on_surface", "on_surface_variant", "outline_variant", "tertiary"}) do
        colors[role] = noctalia.getColor(role)
    end
    local request = {active = opened, hours = hours, finish = anchor or now(), hover = hover,
        colors = colors, device = status.device, error = status.error}
    local tmp = requestPath .. ".tmp"
    if noctalia.writeFile(tmp, noctalia.json.encode(request)) then
        noctalia.renameFile(tmp, requestPath)
    end
end

local function render()
    if not opened then return end
    local controls = {}
    for _, value in ipairs({1, 6, 12, 24}) do
        table.insert(controls, ui.button({key = "range-" .. value, text = value .. " ч", selected = hours == value,
            variant = hours == value and "primary" or "secondary", controlSize = "sm", flexGrow = 1,
            onClick = function() hours = value; hover = nil; sendRequest(); render() end}))
    end
    local label = anchor and os.date("%d.%m %H:%M", anchor - hours * 3600)
        .. " — " .. os.date("%d.%m %H:%M", anchor) or "Последние " .. hours .. " ч"
    local body = {
        ui.row({align = "center", gap = 10}, {
            ui.glyph({name = "chart-line", size = 24, color = "primary"}),
            ui.label({text = "История заряда", fontSize = 21, fontWeight = "bold", flexGrow = 1}),
            ui.button({glyph = "x", variant = "ghost", controlSize = "sm", tooltip = "Закрыть", onClick = function() panel.close() end}),
        }),
        ui.row({gap = 6}, controls),
        ui.row({gap = 5, align = "center"}, {
            ui.button({glyph = "chevron-left", controlSize = "sm", variant = "ghost", tooltip = "Предыдущий период",
                onClick = function() anchor = (anchor or now()) - hours * 3600; hover = nil; sendRequest(); render() end}),
            ui.button({glyph = "calendar", text = label, fontSize = 12, flexGrow = 1, controlSize = "sm", variant = "ghost",
                onClick = function() calendar = not calendar; render() end}),
            ui.button({glyph = "chevron-right", controlSize = "sm", variant = "ghost", enabled = anchor ~= nil and anchor < now(),
                tooltip = "Следующий период", onClick = function()
                    anchor = math.min((anchor or now()) + hours * 3600, now())
                    if anchor >= now() - 1 then anchor = nil end
                    hover = nil; sendRequest(); render()
                end}),
            ui.button({text = "Сейчас", controlSize = "sm", variant = "secondary", onClick = function()
                anchor = nil; calendar = false; hover = nil; sendRequest(); render()
            end}),
        }),
    }
    if calendar then
        local labels = {}
        for _, day in ipairs(days) do table.insert(labels, day.label) end
        table.insert(body, ui.row({gap = 8, align = "center"}, {
            ui.select({key = "date-picker", options = labels, selectedIndex = dateIndex - 1, flexGrow = 1,
                controlSize = "sm", onChange = function(value) dateIndex = tonumber(value) + 1 end}),
            ui.button({text = "Показать день", controlSize = "sm", variant = "primary", onClick = function()
                local day = days[dateIndex]
                if day then
                    hours = 24; anchor = day.finish; calendar = false; hover = nil
                    sendRequest(); render()
                end
            end}),
        }))
    end
    table.insert(body, ui.pixelStream({key = "battery-chart", path = socketPath, width = 520, height = 285,
        onPointerMove = function(x, y) hover = {tonumber(x), tonumber(y)}; sendRequest() end}))
    table.insert(body, ui.row({gap = 14, align = "center"}, {
        ui.label({text = "● Разряд", color = "primary", fontSize = 12}),
        ui.label({text = "● Зарядка", color = "#83c998", fontSize = 12}),
        ui.label({text = "┄ Нет измерений", color = "on_surface_variant", fontSize = 12}),
    }))
    table.insert(body, ui.label({text = status.error or status.warning or "История за 30 дней · скорость за 15 минут", color = "on_surface_variant", fontSize = 11}))
    panel.render(ui.column({gap = 10, padding = 8}, body))
end

noctalia.state.watch("status", function(value)
    status = value or {}; days = status.days or days
    if opened then sendRequest(); render() end
end)

function onOpen()
    opened = true; hours = 24; anchor = nil; calendar = false; hover = nil; dateIndex = 1
    status = noctalia.state.get("status") or {}; days = status.days or {}
    local token = tostring(noctalia.nowMs())
    requestPath = noctalia.pluginDataDir() .. "/view-" .. token .. ".json"
    socketPath = (noctalia.getenv("XDG_RUNTIME_DIR") or "/tmp") .. "/battery-history-" .. token .. ".sock"
    sendRequest()
    noctalia.runStream("python3 " .. quote(noctalia.pluginDir() .. "/battery_history.py")
        .. " render " .. quote(noctalia.pluginDataDir())
        .. " " .. quote(requestPath) .. " " .. quote(socketPath), function(line)
            noctalia.log("Battery graph: " .. line)
        end)
    noctalia.setUpdateInterval(1000)
    render()
end

function update()
    if opened then sendRequest() end
end

function onClose()
    opened = false; sendRequest()
end

function onExit()
    opened = false; sendRequest()
end
