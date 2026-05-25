 nmcli connection show Airtel_Dinesh_5GHz | grep -i dns
connection.mdns:                        -1 (default)
connection.dns-over-tls:                -1 (default)
ipv4.dns:                               --
ipv4.dns-search:                        --
ipv4.dns-options:                       --
ipv4.dns-priority:                      0
ipv4.routed-dns:                        -1 (default)
ipv4.ignore-auto-dns:                   no
ipv6.dns:                               --
ipv6.dns-search:                        --
ipv6.dns-options:                       --
ipv6.dns-priority:                      0
ipv6.routed-dns:                        -1 (default)
ipv6.ignore-auto-dns:                   no
IP4.DNS[1]:                             192.168.1.1
IP6.DNS[1]:                             2401:4900:50:9::280
IP6.DNS[2]:                             2401:4900:50:9::220
➜  arr-stack nmcli connection modify Airtel_Dinesh_5GHz  ipv4.dns "8.8.8.8,1.1.1.1"
Error: Failed to modify connection 'Airtel_Dinesh_5GHz': Insufficient privileges
➜  arr-stack sudo nmcli connection modify Airtel_Dinesh_5GHz  ipv4.dns "8.8.8.8,1.1.1.1"
[sudo: authenticate] Password:
➜  arr-stack sudo nmcli connection modify Airtel_Dinesh_5GHz  ipv4.ignore-auto-dns yes
➜  arr-stack nmcli connection show Airtel_Dinesh_5GHz | grep -i dns
connection.mdns:                        -1 (default)
connection.dns-over-tls:                -1 (default)
ipv4.dns:                               8.8.8.8,1.1.1.1
ipv4.dns-search:                        --
ipv4.dns-options:                       --
ipv4.dns-priority:                      0
ipv4.routed-dns:                        -1 (default)
ipv4.ignore-auto-dns:                   yes
ipv6.dns:                               --
ipv6.dns-search:                        --
ipv6.dns-options:                       --
ipv6.dns-priority:                      0
ipv6.routed-dns:                        -1 (default)
ipv6.ignore-auto-dns:                   no
IP4.DNS[1]:                             192.168.1.1
IP6.DNS[1]:                             2401:4900:50:9::280
IP6.DNS[2]:                             2401:4900:50:9::220


sudo nmcli con down Airtel_Dinesh_5GHz && sudo nmcli con up Airtel_Dinesh_5GHz



sudo nmcli connection modify Airtel_Dinesh_5GHz ipv6.ignore-auto-dns yes
sudo nmcli connection modify Airtel_Dinesh_5GHz ipv6.method ignore
nmcli connection down Airtel_Dinesh_5GHz
nmcli connection up Airtel_Dinesh_5GHz
