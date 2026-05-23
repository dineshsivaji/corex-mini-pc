
# Wiring: ESP32 (D4 + 3V3) → Optocoupler → Enclosure

# Input side (ESP32 → Opto)

# ┌───────────┬─────────────────┬────────────────────────────────────────────────────┐

# │ ESP32 Pin │    Opto Pin     │                      Purpose                       │

# ├───────────┼─────────────────┼────────────────────────────────────────────────────┤

# │ D4        │ IN1             │ High-side switch — D4 outputs 3.3V to power LED    │

# ├───────────┼─────────────────┼────────────────────────────────────────────────────┤

# │ GND       │ G (next to IN1) │ Constant ground path — completes the circuit loop  │

# └───────────┴─────────────────┴────────────────────────────────────────────────────┘

# Output side (Opto → Enclosure)

# ┌────────────────┬───────────────┬───────────────────────────┐

# │    Opto Pin    │ Enclosure Pad │          Purpose          │

# ├────────────────┼───────────────┼───────────────────────────┤

# │ V1             │ A             │ Phototransistor collector │

# ├────────────────┼───────────────┼───────────────────────────┤

# │ G (next to V1) │ D             │ Phototransistor emitter   │

# └────────────────┴───────────────┴───────────────────────────┘

from machine import Pin
import time

# Define the GPIO pin connected to the Optocoupler Input 1 (IN1)
# Using GPIO 4 based on our wiring setup
SOUNCE_TRIGGER_PIN = 4

# Initialize the pin as an OUTPUT and ensure it starts LOW (off)
sounce_switch = Pin(SOUNCE_TRIGGER_PIN, Pin.OUT)
sounce_switch.value(0)

print("ESP32 Boot completed.")
print("Initiating automatic drive spin-up sequence...")

# 20-Second Countdown Timer
print("Get your probes ready!")
for i in range(20, 0, -1):
    # \r moves the cursor to the start of the line, end="" prevents a newline
    print(f"\rRelay will fire in: {i} seconds... ", end="")
    time.sleep(1)
    
print("\nSimulating button press: CLOSING CIRCUIT (LED ON)")
# Turn the pin HIGH (3.3V) to light up the internal optocoupler LED
sounce_switch.value(1)

pressing_time = 2
print("\nPressing button for " + str(pressing_time) + "seconds")
# Your golden 1.5-second hold time required by the Sounce firmware
time.sleep(pressing_time)

print("Simulating button press: OPENING CIRCUIT (LED OFF)")
# Turn the pin LOW (0V) to turn off the LED and release the switch
sounce_switch.value(0)

print("Sequence complete! Sounce enclosure should now be booting up.")


