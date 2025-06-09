import uasyncio as asyncio
import network
import utime as time
import ntptime
import usocket as socket
from machine import Pin, WDT, reset
import urequests
import ujson
import gc

last_fire_alert_time = 0

# ตั้งค่าการเชื่อมต่อ Wi-Fi
WIFI_SSID = 'Devel Document'
WIFI_PASSWORD = ''

# ตั้งค่า Telegram Bot
TELEGRAM_BOT_TOKEN = '7927093931:AAEmpptGqjnVmOUb9akaPvJhxPIvR47nkMg'
TELEGRAM_CHAT_ID = '7807667452'
TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'

# กำหนดขา GPIO สำหรับแต่ละโซน
ZONE_PINS = [
    ("บก.กพช.อร. ชั้น 1", Pin(12, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 1", Pin(13, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 1", Pin(32, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 2", Pin(33, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 2", Pin(25, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 3", Pin(26, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 3", Pin(27, Pin.IN, Pin.PULL_UP)),
    ("บก.กพช.อร. ชั้น 3", Pin(14, Pin.IN, Pin.PULL_UP))
]

# ตัวแปร global
notified_08 = False
notified_18 = False
previous_states = {zone: pin.value() for zone, pin in ZONE_PINS}
wdt = WDT(timeout=120000)  # Watchdog timer 120 วินาที

def get_thailand_time():
    """คำนวณเวลาในประเทศไทย (UTC+7) พร้อมการตรวจสอบข้อผิดพลาด"""
    try:
        current_time = time.localtime()
        if len(current_time) >= 6:
            hour = (current_time[3] + 7) % 24
            # Calculate day adjustment correctly
            day_offset = 0
            if (current_time[3] + 7) >= 24:
                day_offset = 1

            # This part needs careful handling of month/year overflow if day_offset is applied directly
            # For simplicity, we'll assume time.mktime and time.localtime can handle it
            # or that the ESP32's time.localtime already gives data that can be adjusted this way for a simple offset.
            # A more robust way would be to convert to epoch, add 7 hours, then convert back.
            # However, given typical MicroPython limitations, this direct adjustment is common.

            # Get current date parts
            year, month, day_of_month = current_time[0], current_time[1], current_time[2]

            # Create a temporary tuple for mktime (second, minute, hour, mday, mon, year, wday, yday, isdst)
            # We need to reconstruct the date if hour + 7 rolls over midnight

            # Simple approach for now, assuming it doesn't cross month/year boundaries often with +7 hours
            # or that the underlying RTC handles it. If issues arise, this needs to be more robust.
            temp_day = day_of_month
            if (current_time[3] + 7) >= 24:
                 # This is a simplification. Correctly handling date rollovers across months/years
                 # requires knowing days in month, leap years, etc.
                 # MicroPython's time functions might not be as full-featured as CPython's datetime.
                 # For now, we'll assume the simple increment is sufficient for this application's display needs.
                 temp_day = day_of_month + 1
                 # TODO: Add full date rollover logic if required by application precision needs.

            return '{:04}-{:02}-{:02} {:02}:{:02}:{:02}'.format(
                year, month, temp_day, hour, current_time[4], current_time[5])
    except Exception as e:
        print("Error calculating time:", e)
    return "เวลาไม่สามารถระบุได้"

async def send_telegram_message(message):
    """ส่งข้อความผ่าน Telegram Bot พร้อมการตรวจสอบและระบบลองใหม่"""
    if not message or not isinstance(message, str):
        print("ERROR: Invalid message - not a string")
        return False
    message = message.strip()
    if message == "":
        print("ERROR: Message is empty after trimming")
        return False

    print(f"Preparing to send message (length {len(message)}): {message[:50]}...")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not await check_internet():
                print("No internet connection, retrying...")
                await asyncio.sleep(5)
                continue

            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }
            payload_json = ujson.dumps(payload)
            payload_bytes = payload_json.encode('utf-8')
            headers = {'Content-Type': 'application/json; charset=utf-8'}

            response = urequests.post(
                TELEGRAM_API_URL,
                data=payload_bytes,
                headers=headers,
                timeout=10  # Added timeout
            )

            if response.status_code == 200:
                print("Message sent successfully")
                response.close()
                return True
            else:
                error_msg = f"Telegram API error ({response.status_code}): "
                try:
                    error_data = response.json()
                    error_description = error_data.get('description', 'No description').strip()
                    error_msg += error_description
                    if error_description == "" or "message text is empty" in error_description.lower():
                        print("ERROR: Telegram rejected empty message")
                        response.close()
                        return False
                except Exception as e: # Changed from specific ujson.JSONDecodeError which might not exist
                    error_text = response.text if response.text else "No additional error text."
                    error_msg += error_text
                print(error_msg)
                response.close()

        except OSError as e:
            if 'ETIMEDOUT' in str(e) or 'errno 110' in str(e): # ETIMEDOUT for network, 110 can also be timeout
                print(f"Timeout occurred (attempt {attempt+1}), retrying...")
            else:
                print(f"Network error (attempt {attempt+1}):", e)
        except Exception as e:
            print(f"Unexpected error in send_telegram_message (attempt {attempt+1}):", e)

        if attempt < max_retries - 1:
            await asyncio.sleep(5)

    print(f"Failed to send message after {max_retries} attempts")
    return False

async def connect_wifi(max_retries=5):
    """เชื่อมต่อ Wi-Fi พร้อมตรวจสอบการเข้าถึงอินเทอร์เน็ต"""
    sta_if = network.WLAN(network.STA_IF)
    if not sta_if.active():
        sta_if.active(True)

    for attempt in range(max_retries):
        if sta_if.isconnected():
            print("Wi-Fi connected. Checking internet...")
            try:
                # Test with a reliable, commonly available host for DNS resolution check
                socket.getaddrinfo("www.google.com", 80)
                print("Internet access confirmed.")
                return True
            except OSError as e:
                print(f"Wi-Fi connected but no internet (getaddrinfo failed: {e}). Reconnecting...")
                sta_if.disconnect()
                await asyncio.sleep(2) # Short delay before retrying connection
                # Continue to the connection attempt part of the loop
                # Fall through to sta_if.connect()

        print(f'Connecting to Wi-Fi: {WIFI_SSID} (Attempt {attempt+1}/{max_retries})')
        sta_if.connect(WIFI_SSID, WIFI_PASSWORD)

        # Wait for connection, up to 10 seconds
        for _ in range(10):
            if sta_if.isconnected():
                print('Wi-Fi connected! Verifying internet access...')
                try:
                    socket.getaddrinfo("www.google.com", 80)
                    print('Internet access verified!')
                    # Log IP address
                    # print('IP Address:', sta_if.ifconfig()[0])
                    return True
                except OSError as e:
                    print(f"Wi-Fi connected but no internet (getaddrinfo failed: {e}). Will retry connection process.")
                    sta_if.disconnect() # Disconnect to ensure a fresh attempt
                    await asyncio.sleep(1) # Brief pause
                    break # Break inner loop to retry connect_wifi outer loop
            await asyncio.sleep(1)

        if sta_if.isconnected() and not await check_internet(): # Double check if connected but no internet
             print("Connected but internet check failed post-loop. Disconnecting for retry.")
             sta_if.disconnect()
             await asyncio.sleep(1)


    print('Failed to establish internet connection after multiple attempts.')
    return False

async def sync_ntp_time():
    """ซิงค์เวลาจากเซิร์ฟเวอร์ NTP"""
    if not network.WLAN(network.STA_IF).isconnected():
        print("NTP sync requires Wi-Fi. Skipping.")
        return False
    for attempt in range(3):
        try:
            print(f"Attempting NTP sync (attempt {attempt+1}/3)...")
            ntptime.settime() # Default host: pool.ntp.org
            synced_time = get_thailand_time()
            print('Time synced successfully:', synced_time)
            return True
        except Exception as e:
            print(f'NTP sync failed: {e}')
            if attempt < 2:
                await asyncio.sleep(10) # Wait before retrying NTP
            else:
                print("Failed to sync NTP time after multiple attempts.")
    return False

async def check_internet():
    """ตรวจสอบการเชื่อมต่ออินเทอร์เน็ต"""
    try:
        # Check DNS resolution and connectivity to a reliable host
        socket.getaddrinfo("www.google.com", 80)
        return True
    except OSError:
        return False

async def check_fire_zones():
    """ตรวจสอบสถานะเซ็นเซอร์ทุกโซน และแจ้งเตือนซ้ำทุก 5 วินาทีหากยังมีไฟ"""
    global previous_states, last_fire_alert_time # last_fire_alert_time is now used
    active_fire_zones = [] # Stores names of zones currently on fire

    # Check each zone
    for zone_name, pin_object in ZONE_PINS: # Iterate using names from ZONE_PINS
        try:
            current_state = pin_object.value()
            if current_state == 0: # Fire detected in this zone
                active_fire_zones.append(zone_name)

            # Update previous_states for console logging or other logic
            # It's important that zone_name here matches the key used in previous_states initialization
            previous_states[zone_name] = current_state
        except Exception as e:
            print(f"Error reading sensor for {zone_name}: {e}")

    # If fire is detected in any zone, handle notification logic
    if active_fire_zones:
        current_epoch_time = time.time() # Get current time in seconds (epoch)

        # Check if 5 seconds have passed since the last alert for an ongoing fire
        if (current_epoch_time - last_fire_alert_time) >= 5:
            th_time_str = get_thailand_time() # Get formatted time for the message

            # Construct message listing all currently active fire zones
            message_lines = ["🚨 <b>เตือนภัย! ตรวจพบไฟไหม้ต่อเนื่อง</b>"]
            for zone_n in active_fire_zones:
                message_lines.append(f"📍 ตำแหน่ง: {zone_n}")
            message_lines.append(f"🕒 เวลา: {th_time_str}")
            message_lines.append("โปรดตรวจสอบทันที!")
            alert_msg = "\n".join(message_lines) # Use newline character for Telegram

            print(f"Generated continuous fire alert for zones: {', '.join(active_fire_zones)}")

            if await send_telegram_message(alert_msg):
                print("Continuous fire alert Telegram message sent successfully.")
                last_fire_alert_time = current_epoch_time # Update the time of the last alert
            else:
                print("Failed to send continuous fire alert Telegram message.")

        return True # Indicate that fire is currently active

    return False # No fire detected in any zone

async def send_daily_report():
    """ส่งรายงานประจำวันเวลา 08:00 และ 18:00 น."""
    global notified_08, notified_18

    try:
        th_time_str = get_thailand_time()
        if th_time_str == "เวลาไม่สามารถระบุได้":
            print("Daily report: Cannot get Thailand time.")
            return

        # Extract hour and minute carefully
        parts = th_time_str.split(' ')
        if len(parts) < 2:
            print(f"Daily report: Invalid time string format: {th_time_str}")
            return

        time_parts = parts[1].split(':')
        if len(time_parts) < 2:
            print(f"Daily report: Invalid time format in string: {parts[1]}")
            return

        hour = int(time_parts[0])
        minute = int(time_parts[1])

        # Morning report
        if hour == 8 and minute == 0 and not notified_08:
            msg = f"""📊 <b>รายงานประจำวัน (เช้า)</b>
✅ สถานะระบบ: ปกติ
🕒 เวลา: {th_time_str}
📌 จุดตรวจทั้งหมด: {len(ZONE_PINS)} จุด"""
            print("Sending morning report...")
            if await send_telegram_message(msg):
                notified_08 = True

        # Evening report
        elif hour == 18 and minute == 0 and not notified_18:
            msg = f"""📊 <b>รายงานประจำวัน (เย็น)</b>
✅ สถานะระบบ: ปกติ
🕒 เวลา: {th_time_str}
📌 จุดตรวจทั้งหมด: {len(ZONE_PINS)} จุด"""
            print("Sending evening report...")
            if await send_telegram_message(msg):
                notified_18 = True

        # Reset notification flags past midnight (e.g., at 00:01)
        elif hour == 0 and minute == 1:
            if notified_08 or notified_18:
                 print("Resetting daily notification flags.")
                 notified_08 = False
                 notified_18 = False

    except Exception as e:
        print(f"Error in send_daily_report: {e}")


async def system_monitor():
    """ตรวจสอบสุขภาพระบบ"""
    while True:
        try:
            wdt.feed() # Feed watchdog in system monitor as well

            if not network.WLAN(network.STA_IF).isconnected():
                print("Wi-Fi disconnected! Reconnecting in system_monitor...")
                if not await connect_wifi(): # connect_wifi has its own retry logic
                    print("System_monitor: Wi-Fi reconnection failed.")
                    # Consider sending a critical alert if it fails persistently,
                    # but connect_wifi might already do this or try indefinitely.
                    # For now, just log and system_monitor will retry connection check later.
                else:
                    print("System_monitor: Wi-Fi reconnected.")
                    # Attempt to resync time after reconnect
                    if not await sync_ntp_time():
                        print("Warning: Time sync failed after Wi-Fi reconnect, using local time.")


            # Memory check and garbage collection
            if gc.mem_free() < 20000: # Increased threshold slightly
                print(f"Low memory: {gc.mem_free()} bytes. Collecting garbage...")
                gc.collect()
                print(f"Memory after collection: {gc.mem_free()} bytes.")

            await asyncio.sleep(60)  # Run system monitor tasks every 60 seconds

        except Exception as e:
            print(f"System monitor critical error: {e}")
            # Potentially reset or take other drastic action if system_monitor itself fails badly
            await asyncio.sleep(30) # Shorter sleep on error to retry sooner

async def main_loop():
    """ลูปหลักของระบบ"""
    print("Starting fire detection system...")
    wdt.feed()

    if not await connect_wifi():
        print("Initial Wi-Fi connection failed! System will restart.")
        await send_telegram_message("⚠️ <b>ระบบแจ้งเตือน</b>
การเชื่อมต่อ Wi-Fi เริ่มต้นล้มเหลว ระบบจะรีสตาร์ท")
        await asyncio.sleep(5) # Brief delay before reset
        reset() # Restart the device

    wdt.feed()
    if not await sync_ntp_time():
        print("Warning: NTP Time sync failed during startup, using local time. Alerts may have incorrect timestamps initially.")
        # Send a notice about time sync failure
        await send_telegram_message("⚠️ <b>ระบบแจ้งเตือน</b>
การซิงค์เวลา NTP ล้มเหลวในการเริ่มต้นระบบ เวลาอาจจะไม่ถูกต้องในการแจ้งเตือนเบื้องต้น")


    # Start background tasks
    asyncio.create_task(system_monitor())
    wdt.feed()

    # Send startup message
    startup_msg_base = f"""✅ <b>ระบบตรวจสอบไฟไหม้เริ่มทำงาน</b>
📌 จุดตรวจทั้งหมด: {len(ZONE_PINS)}"""

    # Try to send startup message with current time, fallback if time not available
    th_time_startup = get_thailand_time()
    if th_time_startup == "เวลาไม่สามารถระบุได้":
        startup_msg = startup_msg_base + "
🕒 เวลา: (ไม่สามารถซิงค์เวลาได้)"
    else:
        startup_msg = startup_msg_base + f"
🕒 เวลา: {th_time_startup}"

    # Retry sending startup message
    startup_attempts = 0
    max_startup_attempts = 3
    while startup_attempts < max_startup_attempts:
        if await send_telegram_message(startup_msg):
            print("Startup message sent.")
            break
        else:
            print(f"Failed to send startup message (attempt {startup_attempts+1}/{max_startup_attempts}). Retrying...")
            startup_attempts += 1
            await asyncio.sleep(10) # Wait before retrying startup message
    else: # Executed if the loop completes without break
        print("Critical: Failed to send startup message after multiple attempts.")


    print("Entering main operational loop...")
    while True:
        try:
            wdt.feed() # Feed watchdog at the start of each main loop iteration

            # Check for internet connectivity before critical operations
            if not await check_internet():
                print("Main loop: No internet connection. Attempting to reconnect Wi-Fi...")
                if await connect_wifi(): # connect_wifi has retries
                    print("Main loop: Wi-Fi reconnected.")
                    # Sync time again if Wi-Fi was down
                    if not await sync_ntp_time():
                         print("Warning: Time sync failed after Wi-Fi reconnect in main loop.")
                else:
                    print("Main loop: Wi-Fi reconnection failed. Operations requiring internet may fail.")
                    # If Wi-Fi is critical and repeatedly fails, consider a reset or more drastic action.
                    # For now, loop continues, send_telegram_message has its own checks.

            # Call check_fire_zones; its internal logic now handles alerts and relevant printing.
            # The existing print statement below can be kept for high-level flow indication.
            if await check_fire_zones():
                print("Fire event detected and processed by check_fire_zones.") # Kept for clarity, or can be more generic
            else:
                print("No fire detected by check_fire_zones.") # Optional: print for no fire case


            await send_daily_report()

            print("--- Zone States (from previous_states) ---") # Clarified print
            for zone_name, pin_obj in ZONE_PINS:
                 state_val = previous_states.get(zone_name, 1) # Default to 1 (Normal) if somehow not in previous_states
                 print(f"{zone_name}: {'ALARM' if state_val == 0 else 'Normal'}")
            print("-------------------")

            # gc.collect() # gc.collect() is already in system_monitor. Calling it here too frequently might not be necessary.
                          # Let's remove it from here to rely on system_monitor's GC.
            await asyncio.sleep(1) # MODIFIED: Changed sleep from 10 to 1 second.

        except Exception as e:
            print(f"Main loop critical error: {e}")
            # Attempt to send a generic error message if possible
            try:
                await send_telegram_message(f"⚠️ <b>ระบบแจ้งเตือน</b>
เกิดข้อผิดพลาดร้ายแรงในลูปหลัก: {e}
โปรดตรวจสอบระบบ")
            except Exception as tel_e:
                print(f"Failed to send critical error telegram message: {tel_e}")

            await asyncio.sleep(30) # Wait longer after an error before retrying loop

if __name__ == '__main__':
    try:
        # Initialize previous_states based on current pin values at startup
        # This ensures previous_states is populated before the first call to check_fire_zones
        # Note: Pin objects should be defined before this line. ZONE_PINS should be fully initialized.
        # This was: previous_states = {zone: pin.value() for zone, pin in ZONE_PINS}
        # Let's ensure it's correctly done with the tuple structure of ZONE_PINS
        temp_states = {}
        for name, pin_obj in ZONE_PINS:
            try:
                temp_states[name] = pin_obj.value()
            except Exception as e:
                print(f"Error initializing pin state for {name}: {e}")
                temp_states[name] = 1 # Default to normal if read fails
        previous_states = temp_states

        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Program stopped by user.")
        # Optionally send a "stopped by user" message if network is up
    except Exception as e:
        print(f"Critical unhandled error at program root: {e}")
        # Attempt to send a final critical error message
        # This is a last-ditch effort, may not have network or asyncio loop
        # For a more robust solution, consider a separate minimal function for critical alerts
        # if the main async components fail.
        # reset() # Consider if reset is appropriate for all critical errors
