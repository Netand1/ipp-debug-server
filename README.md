# ipp-debug-server

Interface web légère pour **tester, inspecter et déboguer des imprimantes IPP**
(Internet Printing Protocol — RFC 8010 / 8011) directement depuis un navigateur.

Le serveur est un unique fichier Python (Flask) qui encode/décode lui-même le
protocole IPP binaire : aucune dépendance à CUPS ou à `ipptool` n'est requise.

---

## Fonctionnalités

- **Opérations IPP** : `Get-Printer-Attributes`, `Get-Jobs`, `Get-Job-Attributes`,
  `Print-Job`, `Validate-Job`, `Cancel-Job`, `Hold-Job`, `Release-Job`,
  `Restart-Job`, `Pause-Printer`, `Resume-Printer`, `Purge-Jobs`,
  `CUPS-Get-Printers`.
- **Choix de la version IPP** : 1.1, 2.0, 2.1, 2.2.
- **Schémas `ipp://` et `ipps://`** (TLS), avec option pour désactiver la
  vérification du certificat (imprimantes à certificat auto-signé).
- **Affichage des réponses** en arborescence (*Tree View*) ou en **JSON brut**,
  avec traduction lisible des enums (états imprimante/job, codes de statut…).
- **Impression de fichiers** : PDF, PostScript, JPEG, PNG, texte, avec options
  copies, recto/verso (`sides`), couleur (`print-color-mode`), format (`media`).
- **Conversion automatique en PDF** des documents Office via LibreOffice
  (`.docx`, `.doc`, `.odt`, `.rtf`, `.xlsx`, `.xls`, `.ods`, `.pptx`, `.ppt`,
  `.odp`, `.html`, `.csv`).
- **Découverte réseau** des imprimantes :
  - mDNS / Bonjour (`_ipp._tcp`, `_ipps._tcp`) via `zeroconf` ;
  - scan du sous-réseau local sur le port 631 ;
  - interrogation IPP de chaque imprimante trouvée pour récupérer son URI réelle.
- **Historique** des URI récemment utilisées (stocké dans le navigateur).

---

## Prérequis

| Composant      | Obligatoire | Rôle                                         |
|----------------|-------------|----------------------------------------------|
| Python ≥ 3.9   | oui         | exécution du serveur                         |
| Flask, requests| oui         | serveur web et client HTTP                   |
| zeroconf       | non         | découverte mDNS / Bonjour                    |
| LibreOffice    | non         | conversion des documents Office en PDF       |

---

## Installation

### Linux / macOS / WSL

```bash
git clone https://github.com/Netand1/ipp-debug-server.git
cd ipp-debug-server

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optionnel : conversion des documents Office
sudo apt install libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress   # Debian / Ubuntu
```

### Windows (PowerShell)

```powershell
git clone https://github.com/Netand1/ipp-debug-server.git
cd ipp-debug-server

py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Pour la conversion Office sous Windows, installez LibreOffice et ajoutez le
dossier contenant `soffice.exe` au `PATH`.

---

## Lancement

```bash
./run.sh            # utilise automatiquement .venv s'il existe
# ou
python3 app.py
```

Puis ouvrez **http://localhost:5631** dans votre navigateur.

### Configuration

| Variable d'environnement | Défaut    | Description                     |
|--------------------------|-----------|---------------------------------|
| `IPP_GUI_HOST`           | `127.0.0.1` | adresse d'écoute              |
| `IPP_GUI_PORT`           | `5631`      | port HTTP de l'interface      |

Par défaut, l'interface n'est accessible que depuis la machine locale
(sous WSL2, le navigateur Windows y accède quand même via `localhost`).
Pour l'ouvrir aux autres postes du réseau, sur le port 8080 :

```bash
IPP_GUI_HOST=0.0.0.0 IPP_GUI_PORT=8080 ./run.sh
```

---

## Utilisation rapide

1. Saisissez l'URI de l'imprimante, par exemple `ipp://192.0.2.10:631/ipp/print`
   — ou cliquez sur **Discover** pour la rechercher sur le réseau.
2. Choisissez l'opération (par défaut `Get-Printer-Attributes`) puis **Execute**.
3. Parcourez la réponse dans l'onglet **Tree View** ou **JSON**.
4. Pour imprimer, passez sur l'onglet **Print Job**, choisissez un fichier et
   les options souhaitées.

### API HTTP

L'interface s'appuie sur une petite API JSON, utilisable aussi en script.
Chaque appel doit porter l'en-tête **`X-IPP-Debug: 1`** (protection CSRF), et
seules les URI `ipp://` et `ipps://` sont acceptées.

| Méthode | Route            | Description                                         |
|---------|------------------|-----------------------------------------------------|
| `POST`  | `/api/ipp`       | envoie une opération IPP générique (JSON)           |
| `POST`  | `/api/print-job` | envoie un fichier (`multipart/form-data`)           |
| `POST`  | `/api/discover`  | découvre les imprimantes (`mdns`, `scan`)           |

```bash
curl -s http://localhost:5631/api/ipp \
  -H 'X-IPP-Debug: 1' -H 'Content-Type: application/json' \
  -d '{"printer_uri":"ipp://192.0.2.10:631/ipp/print","operation":"get-printer-attributes"}'

curl -s http://localhost:5631/api/print-job \
  -H 'X-IPP-Debug: 1' \
  -F printer_uri=ipp://192.0.2.10:631/ipp/print \
  -F sides=two-sided-long-edge -F color_mode=monochrome \
  -F document=@document.pdf
```

---

## ⚠️ Sécurité

Cet outil est destiné au **débogage sur un réseau de confiance** :

- il n'a **aucune authentification** ;
- toute personne qui y accède peut envoyer des travaux, annuler ou purger des
  jobs, mettre en pause les imprimantes et scanner le sous-réseau.

Protections en place :

- écoute sur `127.0.0.1` uniquement par défaut ;
- en-tête `X-IPP-Debug` obligatoire sur `/api/*` : un site web tiers ouvert
  dans votre navigateur ne peut pas piloter l'API (CSRF) ;
- seules les URI `ipp://` / `ipps://` sont acceptées, ce qui empêche d'utiliser
  le serveur comme relais HTTP générique.

N'exposez l'interface sur le réseau (`IPP_GUI_HOST=0.0.0.0`) que sur un réseau
de confiance.

---

## Licence

[MIT](LICENSE)
