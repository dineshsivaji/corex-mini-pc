Final Network Fix Summary (Saved Configuration)
🔧 1. Switched renderer to NetworkManager

From your config:

network:
  renderer: NetworkManager

✔️ This is the key fix

👉 Meaning:

Netplan now tells the system to use NetworkManager
NOT systemd-networkd
🔧 2. Disabled systemd-networkd (implicit step)

Even though not shown here, you did:

sudo systemctl disable systemd-networkd
sudo systemctl stop systemd-networkd

✔️ Prevented conflict between:

systemd-networkd ❌
NetworkManager ✅
🔧 3. WiFi configured under Netplan → NetworkManager

Your WiFi section:

wifis:
  NM-d67ed3b4-...:
    renderer: NetworkManager
    match:
      name: "wlp3s0"

✔️ This ensures:

Interface wlp3s0 is handled by NetworkManager
Not by low-level services
🔧 4. DHCP enabled (automatic IP assignment)
dhcp4: true
dhcp6: true

✔️ So your system:

Automatically gets IP from router
No manual config needed
🔧 5. WiFi credentials managed properly
access-points:
  "Airtel_Dinesh_5GHz":
    auth:
      key-management: "psk"
      password: "*****"

✔️ NetworkManager handles:

Authentication
Reconnection
Roaming
🔧 6. Netplan + NetworkManager integration (important)

This section:

networkmanager:
  uuid: "..."
  name: "Airtel_Dinesh_5GHz"

✔️ Means:

Netplan is delegating control to NetworkManager
Connection is tracked using UUID







cat /etc/modprobe.d/rtw89.conf
# PCI-level fixes
options rtw89_pci disable_aspm_l1=y
options rtw89_pci disable_aspm_l1ss=y
options rtw89_pci disable_clkreq=y

# Core-level fix (power saving)
options rtw89_core disable_ps_mode=y
