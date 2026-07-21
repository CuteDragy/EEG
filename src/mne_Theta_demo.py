import numpy as np
import mne
import matplotlib.pyplot as plt

print("Initializing the MNE signal processing pipeline...")

# 1. Simulate generating 10 seconds of EEG data (sampling rate 256 Hz)
sfreq = 256  
times = np.arange(0, 10, 1/sfreq)

# We mix three frequency components to simulate complex EEG waves:
# - 2 Hz (Delta wave - slow wave)
# - 6 Hz (Theta wave - our target)
# - 20 Hz (Beta wave - fast wave)
# plus some random Gaussian white noise
signal_delta = np.sin(2 * np.pi * 2 * times)
signal_theta = np.sin(2 * np.pi * 6 * times)
signal_beta = np.sin(2 * np.pi * 20 * times)
noise = np.random.normal(0, 0.5, len(times))

# Sum them into a single-channel raw signal
raw_data = signal_delta + signal_theta + signal_beta + noise
raw_data = raw_data.reshape(1, -1)  # MNE requires data shape (n_channels, n_times)

# 2. Convert the NumPy array to an MNE Raw object
info = mne.create_info(ch_names=['Mock_CH1'], sfreq=sfreq, ch_types=['eeg'])
raw_mne = mne.io.RawArray(raw_data, info)

# 3. Core step: apply a bandpass filter to extract 4-8 Hz Theta waves
# This step matches the new task requirement
filtered_mne = raw_mne.copy().filter(l_freq=4.0, h_freq=8.0, fir_design='firwin')

# 4. Visual comparison
# Plot a simple comparison for the first 2 seconds using matplotlib
time_slice = slice(0, int(2 * sfreq))

plt.figure(figsize=(12, 6))
plt.plot(times[time_slice], raw_data[0][time_slice], label="Raw Signal (Mixed + Noise)", color='lightgray')
plt.plot(times[time_slice], filtered_mne.get_data()[0][time_slice], label="Filtered Theta (4-8 Hz)", color='blue', linewidth=2)

plt.title("MNE-Python: EEG Bandpass Filtering (Theta Wave)")
plt.xlabel("Time (seconds)")
plt.ylabel("Amplitude")
plt.legend()


print("\nFiltering complete! The smooth blue curve is the extracted Theta wave.")

theta_data = filtered_mne.get_data()[0]
theta_power = np.mean(theta_data**2)
theta_rms_amplitude = np.sqrt(theta_power)

print("-" * 30)
print("Theta band (4-8 Hz) feature extraction results:")
print(f"Total data points (N): {len(theta_data)}")
print(f"Root mean square amplitude (RMS): {theta_rms_amplitude:.6f} µV")
print(f"Total signal power (Power): {theta_power:.6f} µV^2")
print("-" * 30)

plt.show()