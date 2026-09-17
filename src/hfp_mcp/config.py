"""
HFP feature flags, D-Bus constants, and audio parameters.
All tuneable values live here.
"""

# HFP 1.8 Hands-Free features bitmask (sent in AT+BRSF, Table 3.7)
# Bit 0 (  1): EC/NR function — no
# Bit 1 (  2): Three-way calling — no
# Bit 2 (  4): CLI presentation capability — yes
# Bit 3 (  8): Voice recognition activation — no
# Bit 4 ( 16): Remote volume control — no
# Bit 5 ( 32): Enhanced call status — yes
# Bit 6 ( 64): Enhanced call control — no
# Bit 7 (128): Codec negotiation (mSBC) — no, keep CVSD/8 kHz for simplicity
HFP_HF_BRSF_FEATURES: int = (1 << 2) | (1 << 5)  # 36

# The SDP Profile1 ``Features`` field uses a different bit assignment from
# AT+BRSF.  Advertise CLI presentation only.  In
# particular SDP bit 5 (wide-band speech) stays clear until mSBC exists.
HFP_SDP_FEATURES: int = 1 << 2  # 4

# One-release import alias. New code must use the transport-specific constant.
HFP_HF_FEATURES: int = HFP_HF_BRSF_FEATURES

# Bluetooth UUIDs
HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"   # Hands-Free (HF side)
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"   # Audio Gateway (phone side)

# D-Bus object paths owned by our process
HFP_PROFILE_PATH = "/org/hfp_mcp/HFProfile"
AGENT_PATH = "/org/hfp_mcp/Agent"

# BlueZ D-Bus service and interfaces
BLUEZ_SERVICE = "org.bluez"
BLUEZ_PROFILE_MANAGER_PATH = "/org/bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"
BLUEZ_ADAPTER_IFACE = "org.bluez.Adapter1"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
BLUEZ_PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"
BLUEZ_AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

# Audio parameters — standard HFP narrow-band (CVSD codec)
AUDIO_SAMPLE_RATE: int = 8000        # Hz
AUDIO_CHANNELS: int = 1
AUDIO_DTYPE: str = "int16"
AUDIO_CHUNK_FRAMES: int = 160        # 20 ms at 8 kHz
AUDIO_BUFFER_MAX_CHUNKS: int = 20    # 400 ms bounded live buffer
AUDIO_STREAM_FRAME_MS: int = 20

# Audio sidecar defaults. MCP remains the control plane; the sidecar carries
# realtime PCM bytes for local voice agents.
AUDIO_STREAM_HOST: str = "127.0.0.1"
AUDIO_STREAM_PORT: int = 8765

# Protocol timeouts
AT_TIMEOUT_SECONDS: float = 5.0
# Some Android audio gateways acknowledge ``ATD`` only after cellular call
# setup has started.  Keep ordinary HFP transactions responsive while giving
# dial a command-specific deadline long enough for those gateways.
AT_DIAL_TIMEOUT_SECONDS: float = 15.0
# Bare HFP terminal responses have no transaction ID. After any timeout, do not
# send another AT command during this drain window; a late response is consumed
# as the orphan from the timed-out command.
AT_TERMINAL_QUARANTINE_SECONDS: float = 5.0
AUDIO_DEVICE_WAIT_SECONDS: float = 10.0
