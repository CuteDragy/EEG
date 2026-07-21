import time
import random

print("EEG mock data source started!\n" + "-"*30)

# Simulate generating 5 EEG data points
for i in range(5):
    # Randomly generate a float between 10.0 and 30.0 to mock microvolts (µV)
    mock_eeg_value = round(random.uniform(10.0, 30.0), 2)

    # Get current timestamp
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")

    # Print to simulate data being emitted
    print(f"[{current_time}] Channel_1 collected data: {mock_eeg_value} µV")

    # Pause 1 second to simulate real-time arrival of data
    time.sleep(1)

print("-"*30 + "\n Test data generation finished!")