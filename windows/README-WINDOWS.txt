PlateBox for Windows, by Icetronix  (https://icetronix.in)
==========================================================

PlateBox reads vehicle number plates from an IP camera and shows them in a web page:
the picture, the number, and every time it came by.

AFTER INSTALLING
  1. PlateBox opens in your browser (or use Start menu > PlateBox). The first time it asks for a SETUP CODE:
     it is the 6 digits in   C:\ProgramData\PlateBox\setup-code.txt   (open it as administrator).
  2. Choose an administrator password. There is only this one account.
  3. Follow the steps: the network, finding the camera, and the settings to type into the camera.

WATCHING THE CAMERA'S VIDEO (recommended, from version 0.2.0)
  PlateBox can open the camera's video itself, the way the Live page does, notice movement, read plates and fill the
  motion log. Nothing then depends on the camera's own picture upload, its clock, its time or name lookups, or the
  internet: only that this computer can reach the camera. Settings > Watching the camera directly: give the camera's
  user name and password once. While it works, PlateBox sets aside the pictures the camera uploads by itself.

THE CAMERA
  The camera sends its pictures to this computer (SFTP, port 2222). The wizard shows exactly what to enter in the
  camera: the type (SFTP), this computer's address, the port, the user name, the password and the folder.
  The camera password is the one you chose while installing (or one PlateBox made up). It never changes when you
  move to another network: only the camera's "Server address" does.

KEEPING THE CAMERA AND THIS COMPUTER IN TOUCH (the address guard)
  Many routers hand out different addresses from time to time. If this computer's address changes, the camera keeps
  sending to the old one and nothing arrives (live view still works, which makes it look like a fault in the
  detection). PlateBox watches for this:
    * when it can, it keeps the camera's old address working by itself (a banner says so; nothing to do);
    * when it cannot, a banner on every page says exactly what to type into the camera, and why.
  The lasting fix is fixed addresses. Pick two unused numbers on the camera's network, outside the router's
  automatic range, for example 192.168.1.200 (this computer) and 192.168.1.201 (the camera), mask 255.255.255.0.
    * This computer: Windows Settings > Network & internet > your Wi-Fi or Ethernet > IP assignment > Manual.
    * The camera: Setting > Network > TCP/IP > Static, with the same mask and the router's address as gateway.
    * The camera's FTP page: Server Address = this computer's fixed address. Save, then test.
  Better still, ask whoever runs the router to reserve those two addresses for them.
  The same page (Settings > Addresses) shows what PlateBox sees.

OLD PICTURES
  The camera asks PlateBox "is this picture already there?" before it sends one, and sends it again if not. So
  PlateBox keeps each picture it has dealt with (12 hours, at most 150 MB, in the locked data folder) and does not
  read it twice. Pictures the camera stamped more than 10 minutes before its own clock now are old ones being sent
  again: they are ignored, not shown as new movement (Settings > Reading changes the 10 minutes; 0 keeps all).
  That goes by the camera's own clock, so keep it right (Setting > System > General > Date & Time).

WHERE THINGS ARE
  Program:          the folder you installed to (normally C:\Program Files\PlateBox)
  Plates, settings: C:\ProgramData\PlateBox   (only administrators can open it; it holds passwords)
  Log file:         C:\ProgramData\PlateBox\logs\platebox.log

IT RUNS IN THE BACKGROUND
  It starts with the computer and keeps running with nobody signed in. There is no window.
  To stop it:   Task Scheduler > PlateBox > End,   or  taskkill /F /IM platebox.exe  (as administrator).
  To remove it: Settings > Apps > PlateBox > Uninstall. You are asked whether to keep the saved plates.

WINDOWS FIREWALL
  The installer allows PlateBox through the firewall for computers on your own network only (not the internet).
  Do not forward ports 80, 8080 or 2222 on your router to this computer.

LIVE VIEW (optional)
  The Live page needs the camera's own user name and password (a view-only camera account is best).
  It runs only while someone is watching.

LIMITS
  The web page is plain HTTP, for a network you trust. Plate reading needs the plate to be wide enough in the
  picture: about 400 pixels or more reads well, under about 200 often does not.
