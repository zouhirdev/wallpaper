### **HOW TO USE:** Convert to EXE

We will use **PyInstaller**.

1.  **Install PyInstaller:**
    Open your terminal/command prompt and run:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Build the EXE:**
    Run this specific command in your terminal (make sure you are in the folder with your script and `icon.ico`).

    ```bash
    pyinstaller --noconsole --onefile --icon=icon.ico --add-data "icon.ico;." bing_clone.py
    ```

    **Explanation of flags:**
    *   `--noconsole`: The black command window won't pop up when you run the app.
    *   `--onefile`: Bundles everything into a single `.exe` file.
    *   `--icon=icon.ico`: Sets the icon of the `.exe` file itself (what you see on desktop).
    *   `--add-data "icon.ico;."`: **Crucial.** This packs the image file *inside* the `.exe` so the program can use it for the tray icon.
        *   *Note: If you are on Mac/Linux, use a colon `:` instead of a semicolon `;` (e.g., `"icon.ico:."`).*

3.  **Locate your App:**
    you can find your .exe inside the dist folder.
    Once finished, go to the **`dist`** folder created in your directory. You will find `bing_clone.exe`.

You can now move this `.exe` anywhere, set it to run on startup, and it will work with the tray icon included!
