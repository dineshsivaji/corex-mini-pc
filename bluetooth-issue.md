 Disabling Bluetooth is 100% safe — WiFi is unaffected. Apply the fix:

  sudo systemctl stop bluetooth
  sudo systemctl disable bluetooth
  echo "blacklist btusb" | sudo tee /etc/modprobe.d/disable-bluetooth.conf
  sudo update-initramfs -u
  sudo reboot
