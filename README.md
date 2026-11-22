# Mocha Pro to Fred exporters

Send your Mocha Pro tracks and roto shapes to Fred as editable nodes.

| Exporter | What you get in Fred |
| --- | --- |
| Fred Roto | One Roto node. In Fred 0.3.2 and newer, each selected Mocha layer gets a folder with the track keyed on the folder. The shapes keep their own keys inside it. Fred 0.2.21 through 0.3.1 get baked shapes with the tracking applied to the points. |
| Fred Tracker | Four point tracks, one for each surface corner. Use **Create Transform** or **Create CornerPin** in Fred to apply the motion. |
| Fred Transform | Keyed position, rotation, and uniform scale. Turn on **Invert** in Mocha to stabilise. |
| Fred CornerPin | Four animated destination corners. The source corners are the full frame. |
| Fred PlanarTrack | An editable surface with four corners per frame and a reference frame. Use it to create a CornerPin in Fred. |

## Requirements

- Mocha Pro 2026.x with Python enabled. The exporters use Mocha Pro 2026.5's bundled Python and Qt.
- Fred 0.3.2 or newer for folder-based Roto. Use Fred 0.2.21 through 0.3.1 for baked Roto. The tracking exporters target Fred 0.2.10 and newer.
- The default target is Fred 0.3.22. Set your version below if you use another release.

## Install

On the GitHub repository page, choose **Code > Download ZIP** and extract it to a folder you can keep. If you use Git, copy the clone URL from **Code** and run `git clone <URL>` with that URL. Keep the four Python files together in the extracted or cloned repository's **mocha_fred** folder.

Choose one of these startup methods. Quit Mocha before making changes. After setup, start Mocha and open a project so the exporters can register.

### Option 1. Add to Mocha's startup script

1. Open the per-user **Scripts** folder for your operating system. On Windows, paste the folder path into File Explorer's address bar. On macOS, use Finder's **Go > Go to Folder**. On Linux, open the path in your file manager or editor.

   | System | Default startup file |
   | --- | --- |
   | Windows | `%APPDATA%\BorisFX\Mocha Pro\Scripts\init.py` |
   | macOS | `~/Library/Application Support/BorisFX/Mocha Pro/Scripts/init.py` |
   | Linux | `~/.config/BorisFX/Mocha Pro/Scripts/init.py` |

2. Back up any existing **init.py** and open it in a plain text editor. Create the folder and file if they are missing.
3. Add these two lines at the end. Replace the example path with the full path to **mocha_fred/init.py** from your download. Use forward slashes in the path, including on Windows. The quotes allow spaces.

   ~~~python
   import runpy
   runpy.run_path("C:/Tools/Mocha to Fred/mocha_fred/init.py")
   ~~~

4. Save **init.py** as UTF-8. If you already use MOCHA_INIT_SCRIPT, add the lines to the **init.py** it selects, or to the script file it points to.

### Option 2. Set MOCHA_INIT_SCRIPT

Set MOCHA_INIT_SCRIPT to the full path of the **mocha_fred** folder. Mocha looks for **init.py** in that folder. You can also set it to the full path to **init.py** itself. This setting overrides the default startup file. If you use that file for other tools, use Option 1 to keep them loading together.

**Windows.** Open **System Properties > Advanced > Environment Variables**. Under **User variables**, click **New**, enter MOCHA_INIT_SCRIPT as the name, and paste the folder's full path as the value without quote marks. You can also run this in Command Prompt. Replace the example path with your own.

~~~bat
setx MOCHA_INIT_SCRIPT "C:\Tools\Mocha to Fred\mocha_fred"
~~~

Start Mocha from the Start menu or a new terminal. An already-open terminal keeps its earlier environment.

**macOS.** In Terminal, run the following with your folder's full path. Then launch Mocha from Finder or the Dock. Run the command again after logging out and back in.

~~~sh
launchctl setenv MOCHA_INIT_SCRIPT "$HOME/Tools/Mocha to Fred/mocha_fred"
~~~

**Linux.** Add this line to your shell profile, such as **~/.profile** for a login shell, or run it in a terminal. Replace the example with your own path. Launch Mocha from that shell once the setting is loaded.

~~~sh
export MOCHA_INIT_SCRIPT="$HOME/Tools/Mocha to Fred/mocha_fred"
~~~

If you start Mocha from a desktop launcher, edit its **Exec** line so it sets the variable. Use the full paths to your **mocha_fred** folder and Mocha executable.

~~~ini
Exec=env "MOCHA_INIT_SCRIPT=/path/to/mocha_fred" "/path/to/Mocha Pro/mocha"
~~~

## Export and paste

1. Open your project in Mocha. Select the layers you want for roto or the tracked layer for tracking. Set the current frame to the reference frame you want for Tracker, Transform, or PlanarTrack.
2. For shapes, choose **File > Export Shape Data** and select **Fred Roto (\*.json)** in the **Fred** group. For tracking, choose **File > Export Tracking Data** and open the **Fred** group. Select **Fred Tracker (\*.json)**, **Fred Transform (\*.json)**, **Fred CornerPin (\*.json)**, or **Fred PlanarTrack (\*.json)**. You can also open these dialogs from the **Track** tab.
3. Click **Copy to Clipboard**. In Fred, click the node graph and press **Ctrl+V** (**Cmd+V** on macOS). Connect the same plate you tracked in Mocha. For Tracker, use **Create Transform** or **Create CornerPin** to make the node that applies its tracking.
4. To use a saved export later, save the **.json** from Mocha. Open it in a plain text editor, copy all the text, and paste it into the Fred node graph the same way.

## Target a Fred version

Set these variables in the environment that starts Mocha. Use the same per-user or launcher method as above. Restart Mocha after changing the settings.

- **FRED_MOCHA_VERSION** sets the target directly. Enter a version such as **0.3.2**.
- **FRED_MOCHA_VERSION_FILE** points to a text file. Enter the full path to the file. Save it as UTF-8 and start its text with a version such as **0.3.2**. A UTF-8 byte order mark, leading whitespace, and a suffix such as **-build1** are accepted.

If FRED_MOCHA_VERSION has any text, the exporters use that setting first and skip the file. The file is read only when FRED_MOCHA_VERSION is empty or unset. With no readable version at the start of the chosen value, the exporters target Fred 0.3.22. They use no default version-file path.

## Troubleshooting

- **The Fred entries are missing.** Restart Mocha and open a project. Check the startup path and make sure the four Python files are still together.
- **Check the Mocha log from the Help menu.** Search for **Fred exporters not registered** and read the message after it. To check Python inside Mocha, open **View > Python Script Editor**, run `print("Python is running")`, and look in its **Output** pane for the result or errors. If a custom PYTHONPATH points to another Python installation, try launching Mocha with PYTHONPATH unset.
- **Mocha reports a missing source exporter.** Folder-based Roto uses Mocha's **Nuke Roto [Transform & Shape] - 6.2+ (\*.nk)**. Baked Roto uses **Shake RotoShape (\*.ssf)**. CornerPin can use **Nuke Corner Pin (\*.nk)** for its tracking options. Check that the relevant built-in exporter is available.
- **Roto reports mixed inversion.** Export the inverted layers in their own pass.
- **The Fred node looks wrong.** Check your target Fred version. Connect a plate with the same resolution and frame numbers as the Mocha project.

## Uninstall

Quit Mocha. If you edited its startup script, remove the two lines you added. If you set MOCHA_INIT_SCRIPT, restore its previous value or remove it from **User variables** on Windows, run `launchctl unsetenv MOCHA_INIT_SCRIPT` on macOS, or remove it from your shell profile or launcher on Linux. Remove any Fred version settings you added. You can now delete the download and restart Mocha.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
