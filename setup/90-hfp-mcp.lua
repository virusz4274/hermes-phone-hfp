-- WirePlumber 0.4: hfp-mcp owns the HFP Hands-Free UUID and SCO transport.
-- Retain A2DP media roles while preventing hfp_hf/hsp_hs registration.
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink a2dp_source ]"
