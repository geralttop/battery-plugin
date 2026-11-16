-- Exercise the real panel callbacks against a minimal Noctalia host.
local tree, request, callbacks
local clock = 1794086000
local stored = {device = "test", days = {{label = "07.11.2026", finish = 1794085200}}}
ui = setmetatable({}, {__index = function(_, kind)
    return function(props, children) return {kind = kind, props = props, children = children} end
end})
noctalia = {
    state = {get = function() return stored end, watch = function(_, cb) callbacks = cb end},
    nowMs = function() return clock * 1000 end,
    pluginDataDir = function() return "/test/data" end,
    pluginDir = function() return "/test/plugin" end,
    getenv = function() return "/test/runtime" end,
    getColor = function() return "#abcdef" end,
    writeFile = function(_, content) request = content; return true end,
    renameFile = function() end,
    json = {encode = function(value) return value end},
    formatTime = function(_, stamp) return tostring(stamp) end,
    runStream = function() end,
    setUpdateInterval = function() end,
    log = function() end,
}
panel = {render = function(value) tree = value end, close = function() onClose() end}
local function find(predicate, node)
    node = node or tree
    if predicate(node) then return node end
    for _, child in ipairs(node.children or {}) do
        local match = find(predicate, child)
        if match then return match end
    end
end
local function click(text)
    local node = assert(find(function(n) return n.props.text == text end), "missing " .. text)
    node.props.onClick()
end
dofile("panel.luau")
onOpen()
assert(request.active and request.hours == 24 and request.finish == clock)
click("6 ч")
assert(request.hours == 6)
find(function(n) return n.props.tooltip == "Предыдущий период" end).props.onClick()
assert(request.finish == clock - 6 * 3600)
find(function(n) return n.props.glyph == "calendar" end).props.onClick()
assert(find(function(n) return n.kind == "select" end))
click("Показать день")
assert(request.hours == 24 and request.finish == stored.days[1].finish)
assert(find(function(n) return n.props.glyph == "calendar" end).props.text:find("07.11", 1, true))
click("Сейчас")
assert(request.finish == clock)
find(function(n) return n.kind == "pixelStream" end).props.onPointerMove("0.5", "0.25")
assert(request.hover[1] == .5 and request.hover[2] == .25)
clock = clock + 60
update()
assert(request.finish == clock)
callbacks({error = "UPower недоступен", days = stored.days})
assert(request.error == "UPower недоступен")
onClose()
assert(not request.active)
onOpen()
assert(request.hours == 24 and not request.hover)
print("Panel navigation, date selection, hover, live refresh and lifecycle: OK")
