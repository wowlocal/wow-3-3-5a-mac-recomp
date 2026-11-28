WrathNative5KReport = { marker = "addon-file" }

local probe = CreateFrame("Frame")

local function record()
    local state = { marker = "in-world" }
    WrathNative5KReport = state
    local names = {
        "farclip", "shadowLevel", "particleDensity", "environmentDetail",
        "groundEffectDensity", "groundEffectDist", "baseMip", "extShadowQuality",
        "textureFilteringMode", "weatherDensity", "componentTextureLevel", "specular",
        "ffxGlow", "ffxDeath", "projectedTextures", "gxResolution", "gxVSync",
        "gxMultisample", "gxColorBits", "gxDepthBits", "maxFPS", "maxFPSBk",
    }
    for _, key in ipairs(names) do
        local ok, found = pcall(GetCVar, key)
        state[key] = ok and found or "unavailable"
    end
    state.marker = "cvars"
    local ok, terrain = pcall(GetTerrainMip)
    if ok then
        state.terrainDetailBefore = terrain
        state.terrainDetail = terrain
    end
    state.formats = {GetMultisampleFormats()}
    state.format = GetCurrentMultisampleFormat()
    state.caps = {GetVideoCaps()}
    if GetCVarMax then
        local valid, limit = pcall(GetCVarMax, "extShadowQuality")
        if valid then state.shadowLimit = limit end
    end
    state.marker = "complete"
end

probe:RegisterEvent("PLAYER_ENTERING_WORLD")
probe:SetScript("OnEvent", function(self)
    self:UnregisterAllEvents()
    record()
end)
