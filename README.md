# EEG Data Engineering Development Workspace

A practical development guide for EEG (Electroencephalography) data engineering projects.

## 1. Common Development Commands

### Run Python Scripts

Run your Python program:

```bash
python your_file_name.py
```

Example:

```bash
python test_run.py
```

### Install New Packages

Install new dependencies only inside the EEG virtual environment:

```bash
pip install <package_name>
```

---

## 2. Development Guidelines

### File Saving

* Save your work frequently during development.
* Use `Ctrl + S` regularly to avoid losing changes.

---

### Virtual Environment

Always activate the `eeg_env` virtual environment before installing packages or running scripts.

#### PowerShell (Recommended)

```powershell
& .\eeg_env\Scripts\Activate.ps1
```

#### Command Prompt (CMD)

```cmd
.\eeg_env\Scripts\activate
```

Make sure the terminal prompt shows:

```text
(eeg_env)
```

before executing any Python commands.

---

### Dependency Management

After installing new packages, update the dependency list:

```bash
pip freeze > requirements.txt
```

Avoid installing packages directly into the global Python environment.

---

### Testing Before Submission

Before committing code, run tests to ensure functionality:

```bash
pytest
```

---

### Git Commit Messages

Use clear and meaningful commit messages.

Examples:

```text
feat: add EEG preprocessing pipeline
fix: correct channel indexing issue
docs: update installation instructions
```

---

### Data Privacy and Security

Do not commit sensitive information to Git repositories.

Never upload:

* Passwords
* API keys
* Private configuration files
* Personal identification information
* Raw personal EEG datasets

Use `.gitignore` to exclude sensitive files.

---

### EEG Data Compliance

When processing EEG or personal data:

* Ensure data has been properly anonymized.
* Confirm that you have appropriate authorization to use the data.
* Follow institutional policies and applicable data protection regulations.

---

### Handling Large Files

GitHub has limitations on file size.

For files larger than 100 MB:

* Use Git LFS (Large File Storage), or
* Store files in external object storage.

Do not directly commit large datasets into the repository.

---

### Python Version Management

Keep the Python version consistent across development environments.

Example:

```text
Python 3.10
```

Document the required version to ensure reproducibility.

---

### Code Formatting and Static Analysis

Before committing code, it is recommended to run:

```bash
black .
isort .
flake8 .
```

These tools help maintain consistent coding style and detect potential issues early.

---

### Reproducibility

To ensure experiments can be reproduced:

* Record dataset sources.
* Document preprocessing steps.
* Save important configuration parameters.
* Set random seeds when necessary.

Example:

```python
import numpy as np

np.random.seed(42)
```

---

### Windows PowerShell Execution Policy

If PowerShell blocks script execution, you may need to update the execution policy:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Only change execution policies when you understand the security implications.

---

# Daily Development Workflow

## Starting the Development Environment

Before coding in VS Code, activate the EEG environment.

### Step 1: Open Terminal

In VS Code:

* Press:

```text
Ctrl + ~
```

or

* Select:

```
Terminal → New Terminal
```

---

### Step 2: Activate Virtual Environment

Make sure you are inside the EEG project directory.

Run:

```cmd
.\eeg_env\Scripts\activate
```

---

### Step 3: Verify Activation

The terminal should display:

```text
(eeg_env)
```

at the beginning of the command line.

Example:

```text
(eeg_env) C:\Projects\EEG>
```

If `(eeg_env)` does not appear, Python packages installed for the project may not be available.

---

# Ending the Development Session

Deactivate the virtual environment:

```bash
deactivate
```

---

## Recommended Project Structure

A typical EEG data engineering project may follow this structure:

```text
EEG_Project/
│
├── data/              # EEG datasets (usually excluded from Git)
├── src/               # Source code
├── tests/             # Unit tests
├── notebooks/         # Experimental notebooks
├── requirements.txt   # Python dependencies
├── README.md          # Project documentation
└── .gitignore         # Ignored files
```
