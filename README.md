# 📁 File Organizer

**A smart, fast, and user-friendly file organizer built with Python.**

File Organizer helps you organize cluttered folders by sorting files into categories based on their file extensions. It includes a terminal interface designed to make file management easier, clearer, and more efficient.

## ✨ Features

* **Automatic Organization** — Sort files into category folders based on file extensions.
* **Preview Mode** — Review planned changes before organizing files, if supported by the selected mode.
* **Custom Categories** — Configure file-extension mappings if enabled in your configuration.
* **Duplicate Handling** — Avoid overwriting files with identical names.
* **Recursive Organization** — Organize files in subfolders if supported by your version.
* **File Search** — Find files using available search options.
* **Duplicate Detection** — Identify potential duplicate files using the implemented detection method.
* **Operation History** — Review recorded organization operations.
* **Undo Support** — Reverse supported operations using the application's undo functionality.
* **Terminal Interface** — Navigate a styled command-line interface.
* **Windows Executable** — Run the packaged application without manually launching its Python source.
* **Logging and Statistics** — Review available operation summaries and diagnostic information.

*Note: Features and options depend on the version you publish. Verify that each listed feature is available in your actual build.*

## 🖥️ Platform

* Windows
* Python, if running from source
* A compatible Windows executable for users who prefer not to install Python

## 🚀 Getting Started

### Option 1: Download the Windows executable

1. Open the repository's **Releases** page.
2. Download `FileOrganizer.exe` from the latest release.
3. Run the executable.
4. Follow the on-screen instructions.

Download: [View Releases](../../releases)

Windows may display a security warning for an unsigned executable. Only run software you trust and have obtained from the intended release.

### Option 2: Run from source

Install a compatible version of Python, then clone the repository:

```bash
git clone https://github.com/YOUR-USERNAME/file-organizer.git
cd file-organizer
```

If the project contains a `requirements.txt` file, install its dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the application using its actual entry point. For example, if `organizer.py` is the entry point:

```bash
python organizer.py
```

Replace `freaky-knight` with your GitHub username. Adjust the commands if your project's actual entry point or dependencies differ.

## 📖 How It Works

1. Select or enter the folder you want to organize.
2. Review the available options.
3. Preview the planned changes if preview mode is available.
4. Confirm the operation when prompted.
5. The application sorts supported files into appropriate categories.
6. Review the summary and operation history.

Always review the target folder and planned changes before executing a large organization operation.

## ⚙️ Configuration

File organization rules may be configurable through the project's configuration file or supported settings.

Depending on the implementation, these rules can specify which file extensions belong to which categories.

For example, a typical organization scheme might look like this:

| Category  | Example extensions      |
| --------- | ----------------------- |
| Documents | `.pdf`, `.docx`, `.txt` |
| Images    | `.png`, `.jpg`, `.jpeg` |
| Videos    | `.mp4`, `.mkv`          |
| Audio     | `.mp3`, `.wav`          |
| Archives  | `.zip`, `.rar`          |
| Code      | `.py`, `.js`, `.html`   |

This table illustrates possible categories; the application's actual mappings depend on its configuration.

## 🔒 Safety

File Organizer is designed to help manage files while keeping operations understandable.

* Review previews before confirming changes.
* Keep backups of important files.
* Check the destination and configuration before organizing large folders.
* Use operation history and undo where supported.
* Do not assume every operation can be reversed in every circumstance.

## 🛠️ Building the Windows Executable

The executable can be built using PyInstaller if the project is configured for it.

Install PyInstaller if required:

```bash
python -m pip install pyinstaller
```

Use the project's verified build command or PyInstaller specification file to create the executable.

Test the generated executable independently before publishing it. Confirm that the terminal interface, configuration, history, and required resources work correctly.

## 🗺️ Roadmap

Potential future improvements:

* Optional lightweight local AI assistance
* More customizable organization rules
* Additional search and filtering options
* Improved reporting and statistics
* More automated testing and release workflows

These are possible future additions, not promises that they are already implemented.

## 🤝 Contributing

Contributions, bug reports, and suggestions are welcome.

1. Fork the repository.
2. Create a branch for your change.
3. Make and test your changes.
4. Submit a pull request describing what changed.

Please include clear reproduction steps when reporting bugs.

## 📄 License

A license has not been specified here. Check the repository for a `LICENSE` file before reusing, modifying, or distributing this project.

## 👨‍💻 Author

Created as a Python project to make everyday file organization easier.

**If you find this project useful, consider giving the repository a ⭐ on GitHub.**
