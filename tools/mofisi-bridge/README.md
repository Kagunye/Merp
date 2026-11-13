# Mofisi bridge — LAN receipt printers and cash drawers

The Mofisi till runs in a web browser, and a browser cannot open a raw network
connection to a printer. The bridge is a tiny program that runs **on the till
PC** and passes the till's print and drawer commands to the printer by its LAN
IP address. The cash drawer plugs into the printer's drawer (RJ11) port, as on
almost every till.

```
Mofisi till (browser)  →  bridge on 127.0.0.1:9180  →  printer 192.168.1.50:9100  →  cash drawer
```

It keeps working **offline**: the till still prints and opens the drawer when
the internet is down.

## Set up

1. Install Python 3.8 or newer on the till PC (python.org; tick "Add to PATH").
2. Copy this folder to the till PC.
3. Edit `start-mofisi-bridge.bat` and put your Mofisi address after `--origin`.
4. Double-click `start-mofisi-bridge.bat`. It says
   `Mofisi bridge 1.0 listening on http://127.0.0.1:9180`.
5. To start it with Windows: Win+R → `shell:startup` → put a shortcut to the
   `.bat` file there.
6. In Mofisi: **POS → Back office → Printers & drawers → New printer**:
   connection *ESC/POS printer on the LAN, via the Mofisi bridge*, the printer's
   IP (print a self-test page on the printer to see it), port 9100, drawer pin 2.
7. On that till PC, open the printers page and press **Use on this device**,
   then **Test print** and **Test drawer**.

## Other printers

* **Epson ePOS** printers (TM-m30, TM-T88VI…) and **Star WebPRNT** printers
  can be driven straight from the browser without the bridge. A Mofisi site on
  HTTPS needs the printer's own HTTPS (Epson: port 8043) or the bridge.
* **Server on the shop LAN:** if Mofisi itself runs on a computer in the shop,
  choose *ESC/POS printer the server itself can reach* and no bridge is needed.

## Safety

* Listens on 127.0.0.1 only: other computers cannot use it.
* Only sends to private addresses (10.x, 172.16–31.x, 192.168.x), never the internet.
* `--origin` limits which website may use it; `--token SECRET` adds a shared secret.
