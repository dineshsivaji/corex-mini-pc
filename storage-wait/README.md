# storage-wait

Block `docker.service` from starting until `/mnt/storage` is actually
mounted. Prevents Docker containers (Immich, Filebrowser, etc.) from
coming up with empty bind mounts when the USB-SATA HDD is still
spinning up after a Mini PC reboot.

## Why

`/etc/fstab` has `nofail,x-systemd.device-timeout=60s` on the HDD
mount, so boot doesn't hang on a slow drive — but that also means
systemd happily starts `docker.service` before the mount is ready.
Docker silently creates `/mnt/storage` on root and mounts that empty
directory into containers, which then report "missing files."

This package inserts a `wait-for-storage.service` between the mount
and Docker. Strict dependency: if the HDD never appears within 120s,
Docker won't start (loud failure beats silent data corruption).

## Files

| File | Installs to | Purpose |
|------|-------------|---------|
| `wait-for-mount.sh` | `/usr/local/bin/wait-for-mount.sh` | Polling script |
| `wait-for-storage.service` | `/etc/systemd/system/wait-for-storage.service` | Oneshot unit |
| `docker-wait-for-storage.conf` | `/etc/systemd/system/docker.service.d/wait-for-storage.conf` | Docker drop-in |

## Deploy

```bash
sudo install -m 755 -o root -g root \
  wait-for-mount.sh /usr/local/bin/wait-for-mount.sh

sudo install -m 644 -o root -g root \
  wait-for-storage.service /etc/systemd/system/wait-for-storage.service

sudo mkdir -p /etc/systemd/system/docker.service.d
sudo install -m 644 -o root -g root \
  docker-wait-for-storage.conf \
  /etc/systemd/system/docker.service.d/wait-for-storage.conf

sudo systemctl daemon-reload
sudo systemctl enable wait-for-storage.service
```

The drop-in approach (vs editing `docker.service` directly) means apt
upgrades of `docker.io` won't clobber our changes.

No restart needed — the dependency takes effect on next boot.

## Verify

```bash
# Sanity: script behavior
sudo /usr/local/bin/wait-for-mount.sh /mnt/storage 5    # exit 0 immediately
sudo /usr/local/bin/wait-for-mount.sh /nonexistent 5    # exit 1 after 5s

# Dependency wired correctly
systemctl list-dependencies docker.service | grep wait-for-storage
systemd-analyze verify wait-for-storage.service

# Real reboot test
sudo reboot
# After reboot:
journalctl -b -u wait-for-storage.service
# Should show: "/mnt/storage ready after Xs"
```

## Failure injection

Simulate a stuck HDD by pointing fstab at a bad UUID:

```bash
sudo cp /etc/fstab /etc/fstab.bak
sudo sed -i 's|^UUID=[a-f0-9-]*|UUID=00000000-0000-0000-0000-000000000000|' \
  /etc/fstab
sudo reboot
```

After boot, `wait-for-storage` should fail at 120s and Docker should
not start. Restore:

```bash
sudo cp /etc/fstab.bak /etc/fstab
sudo reboot
```

## Rollback

```bash
sudo systemctl disable wait-for-storage.service
sudo rm /etc/systemd/system/wait-for-storage.service
sudo rm /etc/systemd/system/docker.service.d/wait-for-storage.conf
sudo rm /usr/local/bin/wait-for-mount.sh
sudo systemctl daemon-reload
sudo reboot
```

## Tunables

The 120s timeout is hard-coded in `wait-for-storage.service`'s
`ExecStart`. To change it, edit the service file:

```bash
sudo systemctl edit --full wait-for-storage.service
# Change: ExecStart=/usr/local/bin/wait-for-mount.sh /mnt/storage 120
# To:     ExecStart=/usr/local/bin/wait-for-mount.sh /mnt/storage 60
sudo systemctl daemon-reload
```

## Limitations

- All Docker containers wait, even ones that don't need `/mnt/storage`
  (HA, AdGuard, etc.). Acceptable — boot delay is only +5–30s typical.
- If the HDD permanently dies, Docker won't start → no HA UI → can't
  receive WhatsApp alerts about it. Detection is "I can't reach HA UI"
  from outside.
