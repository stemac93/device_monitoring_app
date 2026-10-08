import os
import paramiko
import time

def execute_ssh_command(ip, username, password, command):
    """
    Executes a command on a remote device via SSH.
    """
    try:
        ssh = paramiko.SSHClient()
        known_hosts = os.getenv("SSH_KNOWN_HOSTS_FILE")
        if known_hosts:
            # Chiavi note: un host sconosciuto o cambiato viene rifiutato (no MITM)
            ssh.load_host_keys(known_hosts)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # Comportamento storico: accetta qualsiasi chiave. Impostare
            # SSH_KNOWN_HOSTS_FILE per verificare l'identità dei gateway.
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, username=username, password=password, timeout=10)

        stdin, stdout, stderr = ssh.exec_command(command, timeout=10)
        output = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        # L'esito è l'exit status: un warning su stderr non è un fallimento
        exit_status = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_status != 0:
            return False, error or f"exit status {exit_status}"
        return True, output
    except Exception as e:
        return False, str(e)

def set_pin_status(device, pin, status):
    """
    Sets the GPIO pin status on the Orange Pi via SSH.
    """
    # Command to set the GPIO pin
    gpio_status = "1" if status == "on" else "0"

    command = f"gpio mode {int(pin)} out"
    # Execute the command over SSH
    success, response = execute_ssh_command(
        ip=device.ip_address,
        username=device.ssh_username,
        password=device.ssh_password,
        command=command
    )
    if not success:
        return False, f"gpio mode failed: {response}"

    time.sleep(0.5)

    command = f"gpio write {int(pin)} {gpio_status}"
    # Execute the command over SSH
    success, response = execute_ssh_command(
        ip=device.ip_address,
        username=device.ssh_username,
        password=device.ssh_password,
        command=command
    )

    return success, response
