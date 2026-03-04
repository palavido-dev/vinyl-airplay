#!/bin/bash
set -e

# Vinyl Streamer Installer for Raspberry Pi OS and Debian
# This script installs the Vinyl Streamer application and all dependencies

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Utility functions
success() { echo -e "${GREEN}✓${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${YELLOW}•${NC} $1"; }

# Trap for cleanup on error
trap 'error "Installation interrupted"' EXIT INT TERM

# Check if running as root
if [[ $EUID -ne 0 ]]; then
  error "This script must be run as root (sudo)"
fi

UPDATE_MODE=false
if [[ "$1" == "--update" ]]; then
  UPDATE_MODE=true
fi

info "Vinyl Streamer Installer"
echo ""

# Check OS version
if [[ ! $UPDATE_MODE ]]; then
  info "Checking OS compatibility..."
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "$ID" == "raspbian" ]] || [[ "$ID" == "debian" ]]; then
      VERSION_NUM=$(echo "$VERSION_ID" | cut -d. -f1)
      if [[ "$VERSION_NUM" -lt 13 ]] && [[ "$ID" != "raspbian" ]]; then
        error "Debian 13 or later required (found $VERSION_ID)"
      fi
      success "Compatible OS detected: $PRETTY_NAME"
    else
      error "Raspberry Pi OS or Debian 13+ required (found $PRETTY_NAME)"
    fi
  else
    error "Cannot determine OS version"
  fi
fi

info "Installing system dependencies..."
apt-get update || error "Failed to update package lists"
apt-get install -y \
  python3-pip \
  python3-venv \
  ffmpeg \
  libchromaprint-tools \
  portaudio19-dev \
  libasound2-dev \
  bluez \
  bluez-alsa-utils \
  cage \
  wtype \
  chromium-browser \
  hostapd \
  dnsmasq \
  wpa-supplicant \
  git \
  || error "Failed to install system dependencies"
success "System dependencies installed"

info "Creating vinyl-streamer application directory..."
mkdir -p /opt/vinyl-streamer
success "Directory created"

# Clone or update repository
if [[ -d /opt/vinyl-streamer/.git ]]; then
  info "Updating existing repository..."
  cd /opt/vinyl-streamer
  git fetch origin || error "Failed to fetch from origin"
  git reset --hard origin/main || error "Failed to reset repository"
  success "Repository updated"
else
  info "Cloning repository..."
  git clone https://github.com/palavido-dev/vinyl-airplay.git /opt/vinyl-streamer \
    || error "Failed to clone repository"
  success "Repository cloned"
fi

cd /opt/vinyl-streamer

# Create user
if [[ ! $UPDATE_MODE ]]; then
  if ! id "listen" &>/dev/null; then
    info "Creating 'listen' user..."
    useradd -r -s /usr/sbin/nologin -d /opt/vinyl-streamer -m listen || error "Failed to create user"
    usermod -aG audio listen || error "Failed to add user to audio group"
    usermod -aG bluetooth listen || error "Failed to add user to bluetooth group"
    success "User 'listen' created"
  else
    success "User 'listen' already exists"
  fi
fi

# Set up Python virtual environment
info "Setting up Python virtual environment..."
python3 -m venv venv || error "Failed to create virtual environment"
source venv/bin/activate
pip install --upgrade pip setuptools wheel || error "Failed to upgrade pip"
pip install -r requirements.txt || error "Failed to install Python dependencies"
success "Python dependencies installed"

# Create data directory
mkdir -p /opt/vinyl-streamer/data
chown -R listen:listen /opt/vinyl-streamer || error "Failed to set ownership"
chmod 755 /opt/vinyl-streamer
chmod 755 /opt/vinyl-streamer/data

success "Directories configured"

# Set up HTTPS certificates with mkcert
info "Setting up HTTPS certificates for mobile access..."
MKCERT_BIN="/usr/local/bin/mkcert"
CERT_DIR="/opt/vinyl-streamer/certs"
mkdir -p "$CERT_DIR"

if [[ ! -f "$MKCERT_BIN" ]]; then
  MKCERT_VERSION="v1.4.4"
  ARCH=$(uname -m)
  if [[ "$ARCH" == "aarch64" ]]; then
    MKCERT_ARCH="arm64"
  elif [[ "$ARCH" == "x86_64" ]]; then
    MKCERT_ARCH="amd64"
  else
    MKCERT_ARCH="arm"
  fi
  MKCERT_URL="https://dl.filippo.io/mkcert/latest?for=linux/${MKCERT_ARCH}"
  info "Downloading mkcert for ${MKCERT_ARCH}..."
  curl -sSL "$MKCERT_URL" -o "$MKCERT_BIN" || error "Failed to download mkcert"
  chmod +x "$MKCERT_BIN"
  success "mkcert installed"
else
  success "mkcert already installed"
fi

# Generate CA and server certificate
export CAROOT="$CERT_DIR"
if [[ ! -f "$CERT_DIR/rootCA.pem" ]]; then
  info "Creating local Certificate Authority..."
  sudo -u listen CAROOT="$CERT_DIR" "$MKCERT_BIN" -install 2>/dev/null || true
  success "Local CA created"
fi

PI_HOSTNAME=$(hostname)
PI_IP=$(hostname -I | awk '{print $1}')
info "Generating HTTPS certificate for ${PI_HOSTNAME}.local / ${PI_IP}..."
sudo -u listen CAROOT="$CERT_DIR" "$MKCERT_BIN" \
  -cert-file "$CERT_DIR/cert.pem" \
  -key-file "$CERT_DIR/key.pem" \
  "${PI_HOSTNAME}.local" "$PI_HOSTNAME" "$PI_IP" localhost 127.0.0.1 \
  || error "Failed to generate certificate"
chown listen:listen "$CERT_DIR"/*.pem
chmod 644 "$CERT_DIR/cert.pem" "$CERT_DIR/rootCA.pem" 2>/dev/null || true
chmod 600 "$CERT_DIR/key.pem"
success "HTTPS certificates generated"

# Create systemd service for main application
info "Creating systemd service for Vinyl Streamer..."
cat > /etc/systemd/system/vinyl-airplay.service <<'EOF'
[Unit]
Description=Vinyl AirPlay Streamer
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=listen
WorkingDirectory=/opt/vinyl-streamer
ExecStart=/opt/vinyl-streamer/venv/bin/python /opt/vinyl-streamer/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
success "Main service created"

# Create systemd service for WiFi setup
info "Creating systemd service for WiFi setup..."
cat > /etc/systemd/system/vinyl-wifi-setup.service <<'EOF'
[Unit]
Description=Vinyl Streamer WiFi Setup Portal
Before=vinyl-airplay.service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vinyl-streamer
ExecStart=/opt/vinyl-streamer/venv/bin/python /opt/vinyl-streamer/wifi_setup.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
success "WiFi setup service created"

# Create systemd service for Chromium kiosk
info "Creating systemd service for Chromium kiosk..."
cat > /etc/systemd/system/vinyl-kiosk.service <<'EOF'
[Unit]
Description=Vinyl Streamer Kiosk
After=vinyl-airplay.service
Wants=vinyl-airplay.service

[Service]
Type=simple
User=listen
WorkingDirectory=/opt/vinyl-streamer
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/listen/.Xauthority"
ExecStart=/usr/bin/cage -s /usr/bin/chromium-browser --kiosk --no-default-browser-check --no-first-run --disable-sync --disable-translate --disable-infobars --disable-extensions http://localhost:8080
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
EOF
success "Kiosk service created"

# Enable and start services
info "Enabling services..."
systemctl daemon-reload
systemctl enable vinyl-airplay.service || error "Failed to enable main service"
systemctl enable vinyl-wifi-setup.service || error "Failed to enable WiFi setup service"
success "Services enabled"

if [[ ! $UPDATE_MODE ]]; then
  info "Starting services..."
  systemctl start vinyl-airplay.service || error "Failed to start main service"
  systemctl start vinyl-wifi-setup.service || error "Failed to start WiFi setup service"
  success "Services started"
else
  info "Restarting services..."
  systemctl restart vinyl-airplay.service || error "Failed to restart main service"
  success "Services restarted"
fi

trap '' EXIT INT TERM

echo ""
echo "=================================================="
success "Installation complete!"
echo "=================================================="
echo ""
echo "Vinyl Streamer is ready to use."
echo ""
echo "Web UI:    http://<your-pi-ip>:8080"
echo "Mobile:    https://<your-pi-ip>:8443  (install CA cert from Settings for barcode scanning)"
echo "Kiosk:     Enable with: sudo systemctl start vinyl-kiosk.service"
echo ""
echo "Services:"
echo "  Main app:     sudo systemctl status vinyl-airplay"
echo "  WiFi setup:   sudo systemctl status vinyl-wifi-setup"
echo "  Kiosk:        sudo systemctl status vinyl-kiosk"
echo ""
echo "View logs:"
echo "  sudo journalctl -u vinyl-airplay -f"
echo "  sudo journalctl -u vinyl-wifi-setup -f"
echo ""
