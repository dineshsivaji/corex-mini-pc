 # Wiring: ESP32 (D4 + VIN) → Optocoupler → Enclosure

 #  Input side (ESP32 → Opto)

 #  ┌───────────┬─────────────────┬───────────────────────────────────────────────────┐
 #  │ ESP32 Pin │    Opto Pin     │                      Purpose                      │
 #  ├───────────┼─────────────────┼───────────────────────────────────────────────────┤
 #  │ VIN (5V)  │ IN1             │ 5V supply to LED (through onboard resistor)       │
 #  ├───────────┼─────────────────┼───────────────────────────────────────────────────┤
 #  │ D4        │ G (next to IN1) │ Low-side switch — D4 sinks current to turn LED on │
 #  └───────────┴─────────────────┴───────────────────────────────────────────────────┘

 #  Output side (Opto → Enclosure)

 #  ┌────────────────┬───────────────┬───────────────────────────┐
 #  │    Opto Pin    │ Enclosure Pad │          Purpose          │
 #  ├────────────────┼───────────────┼───────────────────────────┤
 #  │ V1             │ A             │ Phototransistor collector │
 #  ├────────────────┼───────────────┼───────────────────────────┤
 #  │ G (next to V1) │ D             │ Phototransistor emitter   │
 #  └────────────────┴───────────────┴───────────────────────────┘

from machine import Pin
import time

OPTO_PIN = 4
PULSE_MS = 1500  # The golden 1.5-second hold time required by your Sounce firmware

# 1. SAFE BOOT STATE: Start the pin as a regular OUTPUT held HIGH (3.3V).
# With 5V hitting IN1 and 3.3V hitting D4, the voltage difference is small (1.7V).
# This prevents the optocoupler LED from accidentally firing during the boot cycle.
sounce_switch = Pin(OPTO_PIN, Pin.OUT, value=1)

print("ESP32 Boot completed.")
print("Initiating automatic drive spin-up sequence (VIN High-Z Logic)...")

# 20-Second Countdown Timer to give you time to position your probes securely
print("Get your probes ready!")
for i in range(20, 0, -1):
    print(f"\rRelay will fire in: {i} seconds... ", end="")
    time.sleep(1)

print("\n\n[!] Simulating button press: CLOSING CIRCUIT (LED ON)")
# 2. TRIGGER THE SWITCH: Re-initialize the pin as an OUTPUT and pull it to 0V (LOW).
# The full 5V from VIN now rushes through the optocoupler LED and sinks directly into D4,
# lighting the LED to maximum, bright saturation.
sounce_switch.init(mode=Pin.OUT, value=0)

# Hold the button down for the enclosure firmware to register it
time.sleep_ms(PULSE_MS)

print("[!] Simulating button press: OPENING CIRCUIT (LED OFF)")
# 3. DISCONNECT THE PATH: Force the pin back into a standard OUTPUT held HIGH (1).
# This cuts off the current loop immediately, turning the optocoupler LED completely OFF.
sounce_switch.init(mode=Pin.OUT, value=1)

print("\nSequence complete! Sounce enclosure circuit has been cleanly released.")

