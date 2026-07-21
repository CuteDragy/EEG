import time
import random
import requests
import json
import os

print("The EEG data sending terminal has been opened!\n" + "-"*30)
print("Press Ctrl+C at any time to trigger the EMERGENCY PAUSE menu.")

# [MODIFIED] Target URL changed to the specific '/upload' endpoint
TARGET_URL = "https://eeg-a37i.onrender.com/upload"
CONFIG_FILE = "eeg_config.json"

# 1. Load Custom Channels configuration dynamically
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'r') as file:
            config_data = json.load(file)
            CHANNELS = config_data.get("custom_channels", [])
            print(f"Loaded {len(CHANNELS)} custom channels.")
    except Exception as e:
        print(f"Error reading config file. Using default channels.")
        CHANNELS = ['Fp1', 'Fp2', 'F7', 'F3', 'Fz', 'F4', 'F8', 'T3', 'C3', 'Cz']
else:
    CHANNELS = ['Fp1', 'Fp2', 'F7', 'F3', 'Fz', 'F4', 'F8', 'T3', 'C3', 'Cz']

CHUNK_SIZE = 5      
MAX_BUFFER = 50     
data_buffer = []    

while True:
    try:
        # 2. Generate mock EEG values for the current timestamp
        mock_eeg_values = {ch: round(random.uniform(10.0, 30.0), 2) for ch in CHANNELS}
        data_buffer.append(mock_eeg_values)
        
        # 3. Process and upload when the buffer reaches CHUNK_SIZE
        if len(data_buffer) >= CHUNK_SIZE:
            
            # [MODIFIED] Convert row-based buffer into a column-oriented dictionary.
            # This perfectly matches the backend's parse_json_file expectation.
            payload = {}
            for ch in CHANNELS:
                payload[ch] = [row[ch] for row in data_buffer]
            
            # [MODIFIED] Add metadata required by the backend MNE parser to avoid errors.
            payload["sfreq"] = 1.0  # Represents 1 Hz sampling rate for our 1-second delay
            payload["is_test_mode"] = True
            
            # [MODIFIED] Save payload to a temporary JSON file on disk
            temp_filename = f"temp_chunk_{int(time.time())}.json"
            with open(temp_filename, 'w') as f:
                json.dump(payload, f)
            
            print(f"[{time.strftime('%H:%M:%S')}] Uploading {temp_filename} ({CHUNK_SIZE} samples)...")
            
            try:
                # [MODIFIED] Send the file using multipart/form-data via the 'files' parameter
                with open(temp_filename, 'rb') as f:
                    response = requests.post(TARGET_URL, files={"file": f}, timeout=10)
                
                # 4. Check response status and clear buffer if successful
                if response.status_code == 200:
                    print("File uploaded and parsed successfully by backend!")
                    data_buffer.clear()
                else:
                    print(f"Upload failed (HTTP {response.status_code}). Keeping data in buffer...")
            
            except Exception as e:
                print(f"Network error: {e}. Keeping data in buffer...")
                
            finally:
                # [MODIFIED] Always delete the temporary file to prevent disk clutter
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
            
            # 5. Prevent memory overflow during prolonged network outages
            if len(data_buffer) > MAX_BUFFER:
                print("Buffer full! Dropping the oldest data chunk.")
                data_buffer = data_buffer[-CHUNK_SIZE:]

        # Wait 1 second before generating the next data point
        time.sleep(1)

    except KeyboardInterrupt:
        print("\n" + "="*40 + "\n EMERGENCY PAUSE TRIGGERED\n" + "="*40)
        choice = input("Enter 'q' to exit, or 'r' to resume: ")
        if choice.lower() == 'q':
            break