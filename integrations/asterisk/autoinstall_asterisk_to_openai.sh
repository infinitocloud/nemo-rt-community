#!/bin/bash

# Exit on any error
set -e

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Function to check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}This script must be run as root (use sudo)${NC}"
        exit 1
    fi
}

# Function to generate a secure random password or username
generate_secure_credential() {
    openssl rand -base64 12 | tr -d '/+=' | head -c 16
}

# Function to get public IP (external services with timeout)
get_public_ip() {
    # Try external services with timeout
    for service in "https://icanhazip.com" "https://ifconfig.me"; do
        PUBLIC_IP=$(curl -s --max-time 5 "$service" 2>/dev/null || true)
        if [ -n "$PUBLIC_IP" ]; then
            echo "$PUBLIC_IP"
            return
        fi
    done
    # Manual input for interactive shells, default for non-interactive
    if [ -t 0 ]; then
        echo -e "${YELLOW}Could not retrieve public IP. Please enter your public IP (or local IP for VM testing):${NC}"
        read -r PUBLIC_IP
        echo "$PUBLIC_IP"
    else
        echo -e "${YELLOW}Could not retrieve public IP. Using default 192.168.1.100 for non-interactive execution.${NC}"
        echo "192.168.1.100"
    fi
}

# Function to get local network CIDR
get_local_net() {
    # Try to detect local network CIDR from interfaces, excluding loopback
    LOCAL_NET=$(ip addr show | grep -oP 'inet \K(?!127\.)\d+\.\d+\.\d+\.\d+/\d+' | head -1 2>/dev/null || true)
    if [ -n "$LOCAL_NET" ]; then
        IP=$(echo "$LOCAL_NET" | cut -d'/' -f1)
        MASK=$(echo "$LOCAL_NET" | cut -d'/' -f2)
					   
        IFS='.' read -r a b c d <<< "$IP"
        NETWORK_ADDR="$a.$b.$c.0/$MASK"  # Assuming /24 for simplicity; adjust if needed
											
										   
											
			
        echo "$NETWORK_ADDR"
        return
    fi
    # Fail with error if detection fails
					 
    echo -e "${RED}Error: Could not detect local network CIDR. Please check your network configuration and try again.${NC}"
    exit 1
						 
		
																																
							 
	  
}

# Main installation function
main() {
    check_root

    echo -e "${CYAN}Starting Asterisk to OpenAI Realtime installation...${NC}"

    # Update system and install prerequisites
    echo -e "${CYAN}Installing prerequisites...${NC}"
    apt update
    apt install -y nodejs npm asterisk git openssl iproute2

    # Generate secure SIP password and ARI credentials
    SIP_PASSWORD=$(generate_secure_credential)
    ARI_USERNAME=$(generate_secure_credential)
    ARI_PASSWORD=$(generate_secure_credential)

    # Configure Asterisk HTTP with localhost binding
    echo -e "${CYAN}Configuring Asterisk HTTP...${NC}"
    cat << EOF >> /etc/asterisk/http.conf
enabled=yes
bindaddr=127.0.0.1
bindport=8088
EOF

    # Configure Asterisk ARI with secure credentials
    echo -e "${CYAN}Configuring Asterisk ARI...${NC}"
    cat << EOF >> /etc/asterisk/ari.conf
[$ARI_USERNAME]
type=user
password=$ARI_PASSWORD
EOF

    # Configure dialplan
    echo -e "${CYAN}Configuring Asterisk dialplan...${NC}"
    cat << EOF >> /etc/asterisk/extensions.conf
[default]
exten => 9999,1,Answer()
same => n,Stasis(asterisk_to_openai_rt)
same => n,Hangup()
EOF

    # Get public IP for pjsip.conf
    echo -e "${CYAN}Retrieving public IP...${NC}"
    PUBLIC_IP=$(get_public_ip)

    # Get local network CIDR
    echo -e "${CYAN}Detecting local network CIDR...${NC}"
    LOCAL_NET=$(get_local_net)

    # Configure SIP Extensions with secure password
    echo -e "${CYAN}Configuring SIP extensions...${NC}"
    cat << EOF >> /etc/asterisk/pjsip.conf
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0
external_media_address=$PUBLIC_IP
external_signaling_address=$PUBLIC_IP
local_net=$LOCAL_NET

[1005]
type=endpoint
context=default
disallow=all
allow=ulaw
auth=1005
aors=1005
direct_media=no
media_use_received_transport=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=auto

[1005]
type=auth
auth_type=userpass
password=$SIP_PASSWORD
username=1005

[1005]
type=aor
max_contacts=2
EOF

    # Restart Asterisk
    echo -e "${CYAN}Restarting Asterisk...${NC}"
    systemctl restart asterisk

    # Clone repository and install dependencies
    echo -e "${CYAN}Cloning repository and installing dependencies...${NC}"
    mkdir -p /opt
    cd /opt
    git clone https://github.com/infinitocloud/asterisk_to_openai_rt_community.git
    cd asterisk_to_openai_rt_community
    npm install

    # Update config.conf with ARI credentials
    echo -e "${CYAN}Updating configuration file...${NC}"
    if [ -f config.conf ]; then
        # Check if ARI_USERNAME and ARI_PASSWORD exist, add if missing
        if grep -q "^ARI_USERNAME=" config.conf; then
            sed -i "s/ARI_USERNAME=.*/ARI_USERNAME=$ARI_USERNAME/" config.conf
        else
            echo "ARI_USERNAME=$ARI_USERNAME" >> config.conf
        fi
        if grep -q "^ARI_PASSWORD=" config.conf; then
            sed -i "s/ARI_PASSWORD=.*/ARI_PASSWORD=$ARI_PASSWORD/" config.conf
        else
            echo "ARI_PASSWORD=$ARI_PASSWORD" >> config.conf
        fi
    else
        echo -e "${RED}Error: config.conf not found in /opt/asterisk_to_openai_rt_community${NC}"
        echo -e "${RED}The repository clone may have failed or config.conf is missing. Please check the repository and try again.${NC}"
        exit 1
    fi

    # Create systemd service
    echo -e "${CYAN}Creating systemd service...${NC}"
    cat << EOF > /etc/systemd/system/asterisk-openai.service
[Unit]
Description=Asterisk to OpenAI Realtime Service
After=network.target asterisk.service

[Service]
ExecStart=/usr/bin/node /opt/asterisk_to_openai_rt_community/index.js
WorkingDirectory=/opt/asterisk_to_openai_rt_community
Restart=always
User=root
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

    # Enable and start the service
    systemctl daemon-reload
    systemctl enable asterisk-openai.service
    systemctl start asterisk-openai.service

    # Summary of configurations with colors
    echo -e "\n${GREEN}${BOLD}Installation Summary:${NC}"
    echo -e "${GREEN}- Installed prerequisites:${NC} nodejs, npm, asterisk, git, openssl, iproute2"
    echo -e "${GREEN}- Configured Asterisk HTTP:${NC} /etc/asterisk/http.conf (bound to 127.0.0.1:8088)"
    echo -e "${GREEN}- Configured Asterisk ARI:${NC} /etc/asterisk/ari.conf (username: $ARI_USERNAME, password: $ARI_PASSWORD)"
    echo -e "${GREEN}- Configured dialplan:${NC} /etc/asterisk/extensions.conf (extension 9999)"
    echo -e "${GREEN}- Configured SIP extension:${NC} /etc/asterisk/pjsip.conf (username: 1005, password: $SIP_PASSWORD)"
    echo -e "${GREEN}- Public IP used:${NC} $PUBLIC_IP"
    echo -e "${GREEN}- Local network CIDR:${NC} $LOCAL_NET"
    echo -e "${GREEN}- Cloned repository:${NC} /opt/asterisk_to_openai_rt_community"
    echo -e "${GREEN}- Installed Node.js dependencies${NC}"
    echo -e "${GREEN}- Updated config.conf:${NC} /opt/asterisk_to_openai_rt_community/config.conf with ARI credentials"
    echo -e "${GREEN}- Created and started systemd service:${NC} asterisk-openai.service"
    echo -e "\n${YELLOW}${BOLD}Service Status:${NC}"
    systemctl status asterisk-openai.service
    echo -e "\n${YELLOW}${BOLD}Next Steps:${NC}"
    echo -e "${RED}${BOLD}1. REQUIRED: Edit /opt/asterisk_to_openai_rt_community/config.conf to add a valid OPENAI_API_KEY using: sudo nano /opt/asterisk_to_openai_rt_community/config.conf${NC}"
    echo -e "${YELLOW}2. Restart the service:${NC} sudo systemctl restart asterisk-openai.service"
    echo -e "${YELLOW}3. Ensure SIP port (5060 UDP) and RTP ports (10000-20000 UDP) are open in your cloud provider's firewall, restricted to your client network${NC}"
    echo -e "${YELLOW}4. Configure SIP client with:${NC}"
    echo -e "${YELLOW}   - Server: $PUBLIC_IP${NC}"
    echo -e "${YELLOW}   - Username: 1005${NC}"
    echo -e "${YELLOW}   - Password: $SIP_PASSWORD${NC}"
    echo -e "${YELLOW}5. Test by making a SIP call to extension 9999 from your 1005 configured extension${NC}"
}

# Execute main function
main
