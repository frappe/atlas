package main

import (
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"time"
)

// udpIdleTimeout ends a UDP flow that has no datagram in either direction for this long.
const udpIdleTimeout = time.Minute

const portsPollInterval = 2 * time.Second

// forwardPorts keeps the forwards equal to the ports metadata attribute, such as 80:8080/tcp,53:5353/udp.
// A removed TCP forward stops accepting, and its open connections continue.
func forwardPorts(metadata *metadataClient) {
	listeners := map[string]io.Closer{}
	applied := ""
	for ; ; time.Sleep(portsPollInterval) {
		value, err := metadata.read("meta-data/attributes/ports")
		if err != nil || value == applied {
			continue
		}
		applied = value

		wanted := map[string]bool{}
		for _, mapping := range strings.FieldsFunc(value, func(r rune) bool { return r == ',' }) {
			wanted[strings.TrimSpace(mapping)] = true
		}
		for mapping, listener := range listeners {
			if !wanted[mapping] {
				_ = listener.Close()
				delete(listeners, mapping)
			}
		}
		for mapping := range wanted {
			if listeners[mapping] != nil {
				continue
			}
			if listeners[mapping], err = forwardPort(mapping); err != nil {
				delete(listeners, mapping)
				fmt.Printf("guest-init: port %q: %v\n", mapping, err)
			}
		}
	}
}

func forwardPort(mapping string) (io.Closer, error) {
	ports, protocol, _ := strings.Cut(mapping, "/")
	listenValue, targetValue, _ := strings.Cut(ports, ":")
	listenPort, listenError := strconv.ParseUint(listenValue, 10, 16)
	targetPort, targetError := strconv.ParseUint(targetValue, 10, 16)
	if listenError != nil || targetError != nil || listenPort == 0 || targetPort == 0 {
		return nil, errors.New("want <port>:<target port>[/tcp|/udp]")
	}
	// The listener also takes 127.0.0.1, so a forward to its own port would reach itself.
	if listenPort == targetPort {
		return nil, errors.New("the target port must differ from the port")
	}

	switch protocol {
	case "", "tcp":
		listener, err := net.ListenTCP("tcp", &net.TCPAddr{Port: int(listenPort)})
		if err != nil {
			return nil, err
		}
		go forwardTCP(listener, &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: int(targetPort)})
		return listener, nil
	case "udp":
		listener, err := net.ListenUDP("udp", &net.UDPAddr{Port: int(listenPort)})
		if err != nil {
			return nil, err
		}
		go forwardUDP(listener, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: int(targetPort)})
		return listener, nil
	default:
		return nil, fmt.Errorf("unknown protocol %q", protocol)
	}
}

func forwardTCP(listener *net.TCPListener, target *net.TCPAddr) {
	for {
		client, err := listener.AcceptTCP()
		if errors.Is(err, net.ErrClosed) {
			return
		}
		if err != nil {
			fmt.Println("guest-init: accept:", err)
			time.Sleep(time.Second)
			continue
		}
		go func() {
			defer client.Close()
			server, err := net.DialTCP("tcp", nil, target)
			if err != nil {
				return
			}
			defer server.Close()

			var copies sync.WaitGroup
			copies.Go(func() { forwardStream(server, client) })
			forwardStream(client, server)
			copies.Wait()
		}()
	}
}

// forwardStream copies one direction and passes on its end, so the other direction can continue.
// A failure closes both connections, so the other direction ends too.
func forwardStream(destination, source *net.TCPConn) {
	if _, err := io.Copy(destination, source); err != nil {
		_ = destination.Close()
		_ = source.Close()
		return
	}
	_ = destination.CloseWrite()
}

// forwardUDP gives each client address its own socket to the target, so replies go back to that client.
func forwardUDP(listener *net.UDPConn, target *net.UDPAddr) {
	var flowsMutex sync.Mutex
	flows := map[netip.AddrPort]*net.UDPConn{}
	buffer := make([]byte, 65535)
	for {
		size, client, err := listener.ReadFromUDPAddrPort(buffer)
		if err != nil {
			return
		}

		flowsMutex.Lock()
		flow := flows[client]
		if flow == nil {
			if flow, err = net.DialUDP("udp", nil, target); err != nil {
				flowsMutex.Unlock()
				continue
			}
			flows[client] = flow
			go func() {
				reply := make([]byte, 65535)
				for {
					replySize, err := flow.Read(reply)
					if err != nil {
						break
					}
					_, _ = listener.WriteToUDPAddrPort(reply[:replySize], client)
					_ = flow.SetReadDeadline(time.Now().Add(udpIdleTimeout))
				}
				flowsMutex.Lock()
				delete(flows, client)
				flowsMutex.Unlock()
				flow.Close()
			}()
		}
		flowsMutex.Unlock()
		_, _ = flow.Write(buffer[:size])
		_ = flow.SetReadDeadline(time.Now().Add(udpIdleTimeout))
	}
}
