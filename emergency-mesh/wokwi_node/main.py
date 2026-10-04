# Single-node interface prototype (MicroPython, runs in the Wokwi simulator).
# Live project: https://wokwi.com/projects/476841677745845249
# Shows what a node looks like in use: OLED status screen, SOS button, status LED.
# Radio relaying is NOT implemented here; the mesh logic lives in /simulation.
#
# Wiring: OLED SSD1306 on I2C (SCL=GPIO22, SDA=GPIO21), button on GPIO14 (pull-up),
# RGB LED on GPIO27 (red), GPIO32 (green), GPIO33 (blue).
# ssd1306.py (OLED driver) and diagram.json (wiring) are in the Wokwi project linked above.

from machine import Pin, I2C
import time
import ssd1306

# -------------------------
# OLED
# -------------------------
i2c = I2C(0, scl=Pin(22), sda=Pin(21))
oled = ssd1306.SSD1306_I2C(128, 64, i2c)

# -------------------------
# Button
# -------------------------
button = Pin(14, Pin.IN, Pin.PULL_UP)

# -------------------------
# RGB LED
# -------------------------
red = Pin(27, Pin.OUT)
green = Pin(32, Pin.OUT)
blue = Pin(33, Pin.OUT)

# SAMPLE location (Velachery, Chennai). A real node stores the coordinates
# surveyed when it is installed.
NODE_LAT = "12.98150"
NODE_LON = "80.21800"

# -------------------------
# Functions
# -------------------------

def rgb(r, g, b):
    red.value(r)
    green.value(g)
    blue.value(b)

def display_online():
    oled.fill(0)
    oled.text("DISASTER BASE", 0, 0)
    oled.text("STATUS: ONLINE", 0, 16)
    oled.text("Waiting...", 0, 32)
    oled.text("Press button", 0, 48)
    oled.show()

def display_signal():
    # Blue = signal being relayed
    rgb(0, 0, 1)

    oled.fill(0)
    oled.text("SIGNAL RECEIVED", 0, 0)
    oled.text("RELAYING...", 0, 16)
    oled.show()

    time.sleep(2)

def display_danger():
    # Red = emergency
    rgb(1, 0, 0)

    oled.fill(0)
    oled.text("!!! DANGER !!!", 0, 0)
    oled.text("LAT:", 0, 18)
    oled.text(NODE_LAT, 0, 28)
    oled.text("LON:", 0, 42)
    oled.text(NODE_LON, 0, 52)
    oled.show()

# -------------------------
# Start
# -------------------------

rgb(0, 1, 0)
display_online()

while True:

    # Button pressed
    if button.value() == 0:

        display_signal()
        display_danger()

        # Wait until button is released
        while button.value() == 0:
            time.sleep(0.05)

        time.sleep(3)

        rgb(0, 1, 0)
        display_online()

    time.sleep(0.05)
