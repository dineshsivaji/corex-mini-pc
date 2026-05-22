# Fix: Disable rtw89 Hardware Scan Offload for RTL8852B

## Problem

```
rtw89_8852be_git 0000:03:00.0: rtw89_hw_scan_offload failed ret -110
```

Firmware times out on background scan requests, causing periodic WiFi stalls and SSH freezes.

## Root Cause

NetworkManager triggers periodic background scans while connected. The rtw89 driver offloads these to firmware via `RTW89_FW_FEATURE_SCAN_OFFLOAD`, but the firmware times out (`-110 = ETIMEDOUT`). Disabling the feature flag forces the driver to fall back to software scanning — slightly slower but reliable.

## Prerequisites

- RTL8852BE (RTL8852B family) device
- Ubuntu (tested on 26.04 LTS, kernel 7.0.0-15-generic)

## Fresh Install (from scratch)

If starting from a new Ubuntu installation:

### 1. Install build dependencies

```bash
sudo apt install dkms bc build-essential git
```

### 2. Clone the driver repo

```bash
git clone https://github.com/morrownr/rtw89.git
cd rtw89
```

### 3. Patch fw.c BEFORE installing

```bash
grep -n "8852B.*SCAN_OFFLOAD" fw.c
# Comment out the matching lines
nano fw.c
```

Change:

```c
    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 29, 0, SCAN_OFFLOAD),
    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 128, 0, SCAN_OFFLOAD_EXTRA_OP),
```

To:

```c
    // __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 29, 0, SCAN_OFFLOAD),
    // __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 128, 0, SCAN_OFFLOAD_EXTRA_OP),
```

> **Note:** Do NOT comment out lines for other chips (RTL8851B, RTL8852A, RTL8852C, RTL8922A, etc.)

### 4. Run the install script

```bash
sudo ./install.sh
```

This handles DKMS registration, build, install, blacklisting in-kernel modules, and updating initramfs — all in one step.

### 5. Reboot

```bash
sudo reboot
```

### 6. Confirm

```bash
# Monitor for ~10 minutes — should produce no output
sudo dmesg -wT | grep scan_offload
```

---

## Patching an existing installation

If the driver is already installed and you're seeing the scan offload error:

## Steps

### 1. Find the scan offload lines in the DKMS source

```bash
grep -n "8852B.*SCAN_OFFLOAD" /usr/src/rtw89-7.1/fw.c
```

You should see something like:

```
874:    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 29, 0, SCAN_OFFLOAD),
881:    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 128, 0, SCAN_OFFLOAD_EXTRA_OP),
```

### 2. Comment out those lines

```bash
sudo nano /usr/src/rtw89-7.1/fw.c
```

Change:

```c
    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 29, 0, SCAN_OFFLOAD),
    __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 128, 0, SCAN_OFFLOAD_EXTRA_OP),
```

To:

```c
    // __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 29, 0, SCAN_OFFLOAD),
    // __CFG_FW_FEAT(RTL8852B, ge, 0, 29, 128, 0, SCAN_OFFLOAD_EXTRA_OP),
```

> **Note:** Do NOT comment out lines for other chips (RTL8851B, RTL8852A, RTL8852C, RTL8922A, etc.)

### 3. Rebuild and install the DKMS module

```bash
sudo dkms remove rtw89/7.1 -k $(uname -r)
sudo dkms build rtw89/7.1 -k $(uname -r)
sudo dkms install rtw89/7.1 -k $(uname -r)
```

### 4. Verify the new module was built

```bash
modinfo /lib/modules/$(uname -r)/updates/dkms/rtw89_core_git.ko.zst | grep srcversion
```

The `srcversion` should be **different** from what `cat /sys/module/rtw89_core_git/srcversion` shows (the currently loaded one).

### 5. Update initramfs

```bash
sudo update-initramfs -u -k $(uname -r)
```

### 6. (Optional) Verify initramfs has the patched module

```bash
sudo unmkinitramfs /boot/initrd.img-$(uname -r) /tmp/initramfs-check
modinfo /tmp/initramfs-check/usr/lib/modules/$(uname -r)/updates/dkms/rtw89_core_git.ko.zst | grep srcversion
```

This should match the new srcversion from step 4.

### 7. Reboot

```bash
sudo reboot
```

### 8. Confirm after reboot

```bash
# Should show the new srcversion
cat /sys/module/rtw89_core_git/srcversion

# Monitor for ~10 minutes — should produce no output
sudo dmesg -wT | grep scan_offload
```

## After future driver updates

If you update the driver (e.g., `git pull` in the source repo), `fw.c` will be overwritten and you'll need to re-apply this patch. Check with:

```bash
grep "8852B.*SCAN_OFFLOAD" /usr/src/rtw89-7.1/fw.c
```

If the lines are uncommented, repeat from step 1.

## Troubleshooting

### srcversion doesn't change after reboot

- Ensure `update-initramfs -u` was run after `dkms install`
- Extract and inspect the initramfs to verify the patched module is inside it
- If using Secure Boot, ensure the rebuilt module is properly signed

### WiFi doesn't reconnect after modprobe

Unload the full module chain (dependencies must be removed first):

```bash
sudo modprobe -r rtw89_8852be_git rtw89_8852b_git rtw89_8852b_common_git rtw89_pci_git rtw89_core_git
sudo modprobe rtw89_8852be_git
```

## References

- Driver repo: https://github.com/morrownr/rtw89
- Error code: `-110` = `ETIMEDOUT` (firmware did not respond within deadline)
- Feature flag: `RTW89_FW_FEATURE_SCAN_OFFLOAD` in `core.h`

