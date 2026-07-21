import numpy as np
import mne
import matplotlib.pyplot as plt
import os

print("Initializing multi-channel EEG batch processing pipeline...")


'''
sfreq = 256      
# Duration set to 20 seconds to provide enough data points for the Delta filter
duration = 20    
n_channels = 4   

times = np.arange(0, duration, 1/sfreq)
ch_names = [f'EEG_CH{i+1}' for i in range(n_channels)]

# Create a 2D matrix of zeros to store data
raw_data = np.zeros((n_channels, len(times)))

for i in range(n_channels):
    # Generate slightly different mixed signals for each channel using random coefficients
    delta = np.sin(2 * np.pi * 2 * times) * np.random.uniform(0.5, 1.5)
    theta = np.sin(2 * np.pi * 6 * times) * np.random.uniform(0.5, 1.5)
    alpha = np.sin(2 * np.pi * 10 * times) * np.random.uniform(0.5, 1.5)
    beta = np.sin(2 * np.pi * 20 * times) * np.random.uniform(0.5, 1.5)
    noise = np.random.normal(0, 0.5, len(times))
    
    # Write the mixed signal to the corresponding channel row
    raw_data[i, :] = delta + theta + alpha + beta + noise

# Convert the 2D NumPy array into an MNE Raw object
info = mne.create_info(ch_names=ch_names, sfreq=sfreq, ch_types=['eeg'] * n_channels)
raw_mne = mne.io.RawArray(raw_data, info)
'''

# [MODIFIED] Point to the actual EDF file path on your computer.
# Update this path to match exactly where your edf is located.
edf_file_path = r"/path/to/your/edf"

if not os.path.exists(edf_file_path):
    print(f"Error: Could not find the file at {edf_file_path}")
    exit()

# [MODIFIED] Load the real EDF file directly into MNE
# preload=True loads the data into RAM, allowing us to apply filters
print(f"Loading real EEG data from {os.path.basename(edf_file_path)}...")
raw_mne = mne.io.read_raw_edf(edf_file_path, preload=True, verbose=False)

# [MODIFIED] Automatically drop non-EEG channels (like "EDF Annotations")
# We only want to process actual brainwave channels
raw_mne.pick(picks='eeg')

# [MODIFIED] Dynamically extract hardware info from the real dataset
sfreq = raw_mne.info['sfreq']
ch_names = raw_mne.ch_names
n_channels = len(ch_names)

print(f"Successfully loaded {n_channels} EEG channels. Sampling rate: {sfreq} Hz.")

eeg_bands = {
    'Delta (0.3-4 Hz)': (0.3, 4.0),
    'Theta (4-8 Hz)': (4.0, 8.0),
    'Alpha (8-13 Hz)': (8.0, 13.0),
    'Beta (13-30 Hz)': (13.0, 30.0)
}

final_features = {}
alpha_filtered_data = None 

print("\nStarting multi-channel full-band feature extraction:")
print("=" * 60)

for band_name, (l_freq, h_freq) in eeg_bands.items():
    
    # Batch filtering: MNE automatically processes all 4 channels simultaneously
    filtered_mne = raw_mne.copy().filter(l_freq=l_freq, h_freq=h_freq, fir_design='firwin', verbose=False)
    filtered_data = filtered_mne.get_data()
    
    if band_name == 'Alpha (8-13 Hz)':
        alpha_filtered_data = filtered_data
    
    final_features[band_name] = {}
    print(f"Current extraction band: {band_name}")
    
    for ch_idx in range(min(4, n_channels)):
        ch_data = filtered_data[ch_idx] 
        power = np.mean(ch_data**2)
        rms = np.sqrt(power)
        
        final_features[band_name][ch_names[ch_idx]] = {'RMS_Amplitude': round(rms, 4), 'Power': round(power, 4)}
        print(f"   [{ch_names[ch_idx]}] RMS Amplitude: {rms:.4f} µV | Power: {power:.4f} µV^2")
        
    if n_channels > 4:
        print(f"   ... and {n_channels - 4} more channels processed silently.")
    print("-" * 60)

print("\n Full-band multi-channel processing completed!")

# ==========================================
# Step 4: Core demonstration effect - Multi-channel visualization
# ==========================================
print("Generating visual comparison charts...")

# Only slice the first 3 seconds of data for plotting, otherwise the waves are too dense
plot_channels = min(4, n_channels)
plot_samples = int(3 * sfreq) 
raw_data_matrix = raw_mne.get_data()
times = raw_mne.times[:plot_samples]

fig, axes = plt.subplots(plot_channels, 1, figsize=(10, 8), sharex=True)
fig.suptitle(f'Real EEG Signal Processing ({os.path.basename(edf_file_path)})', fontsize=14)

if plot_channels == 1:
    axes = [axes]

for i in range(plot_channels):
    # Plot raw mixed signal (gray)
    axes[i].plot(times, raw_data_matrix[i, :plot_samples], color='lightgray', label='Raw Signal')
    # Plot extracted Alpha wave signal (blue)
    axes[i].plot(times, alpha_filtered_data[i, :plot_samples], color='blue', linewidth=1.5, label='Filtered Alpha (8-13 Hz)')
    
    axes[i].set_ylabel(f'CH{i+1}\nAmp', fontsize=10)
    if i == 0:
        axes[i].legend(loc='upper right')

axes[-1].set_xlabel('Time (seconds)', fontsize=12)
plt.tight_layout()
plt.show()