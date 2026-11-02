"""OpenSSH transport via an existing SOCKS listener, with direct fallback.
Does not modify any proxy, routing, firewall or SSH trust configuration.
"""
import select,socket,struct,sys,threading

def exact(sock,size):
    data=b''
    while len(data)<size:
        value=sock.recv(size-len(data))
        if not value:raise OSError('Connection closed')
        data+=value
    return data

def connect(host,port,proxy_port=7892):
    sock=None
    try:
        sock=socket.create_connection(('127.0.0.1',proxy_port),timeout=6);sock.settimeout(15)
        sock.sendall(b'\x05\x01\x00')
        if exact(sock,2)!=b'\x05\x00':raise OSError('SOCKS authentication unavailable')
        name=host.encode('idna');sock.sendall(b'\x05\x01\x00\x03'+bytes([len(name)])+name+struct.pack('!H',port))
        header=exact(sock,4)
        if header[1]!=0:raise OSError('SOCKS connection rejected')
        size={1:4,4:16}.get(header[3])
        if size is None:size=exact(sock,1)[0]
        exact(sock,size+2);sock.settimeout(None);return sock
    except OSError:
        if sock:sock.close()
        return socket.create_connection((host,port),timeout=12)

if __name__=='__main__':
    channel=connect(sys.argv[1],int(sys.argv[2]));channel.settimeout(None)
    def upload():
        try:
            while True:
                block=sys.stdin.buffer.read1(65536)
                if not block:break
                channel.sendall(block)
            channel.shutdown(socket.SHUT_WR)
        except OSError:pass
    threading.Thread(target=upload,daemon=True).start()
    try:
        while True:
            block=channel.recv(65536)
            if not block:break
            sys.stdout.buffer.write(block);sys.stdout.buffer.flush()
    except OSError:pass
