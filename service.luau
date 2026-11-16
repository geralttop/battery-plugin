local busy = false
noctalia.setUpdateInterval(60000)

function update()
    if busy then return end
    busy = true
    local dir = noctalia.pluginDataDir()
    noctalia.runAsync({"python3", noctalia.pluginDir() .. "/battery_history.py", "collect", dir}, function(result)
        busy = false
        if result.exitCode ~= 0 then
            noctalia.log("Battery history: " .. result.stderr)
            noctalia.state.set("status", {error = "Не удалось прочитать историю батареи"})
            return
        end
        local ok, value = pcall(noctalia.json.decode, result.stdout)
        if ok then noctalia.state.set("status", value) end
    end)
end

update()
