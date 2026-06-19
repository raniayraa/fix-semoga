package maps

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"net"
	"time"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/ringbuf"
	"github.com/telmat/xdp-go/internal/db"
)

// PktAction mirrors enum pkt_action in common_kern_user.h.
const (
	PktActionDrop        = uint8(0)
	PktActionPass        = uint8(1)
	PktActionTX          = uint8(2)
	PktActionRedirect    = uint8(3)
	PktActionTTLExceeded = uint8(4)
)

// packetEvent mirrors struct packet_event (24 bytes) in common_kern_user.h.
// Fields are read in little-endian order as written by the kernel on x86-64.
// IP addresses are stored in network (big-endian) byte order by the kernel.
type packetEvent struct {
	TimestampNs uint64
	SrcIP       uint32
	DstIP       uint32
	SrcPort     uint16
	DstPort     uint16
	Protocol    uint8
	Action      uint8
	PktLen      uint16
	Pad         [4]uint8
}

func ip4String(n uint32) string {
	b := make([]byte, 4)
	binary.LittleEndian.PutUint32(b, n)
	return net.IP(b).String()
}

func toTrafficLog(ev packetEvent) db.TrafficLog {
	return db.TrafficLog{
		TimestampNs: time.Now().UnixNano(),
		SrcIP:       ip4String(ev.SrcIP),
		DstIP:       ip4String(ev.DstIP),
		SrcPort:     int(ev.SrcPort),
		DstPort:     int(ev.DstPort),
		Protocol:    int(ev.Protocol),
		Action:      int(ev.Action),
		PktLen:      int(ev.PktLen),
	}
}

// ConsumeRingBuf reads packet events from the BPF ring buffer and persists
// them to SQLite in batches. It blocks until ctx is cancelled.
//
// Sampling note: the kernel program only emits events to the ring buffer
// selectively — DROP and TTL_EXCEEDED are always emitted (security events),
// while PASS/TX/REDIRECT are sampled at 1 per SAMPLE_RATE packets per CPU.
// Traffic log counts in the DB therefore represent sampled data for forwarded
// traffic, but complete data for dropped/blocked packets.
//
// Batching strategy: flush every 100ms or when 500 events accumulate,
// whichever comes first. This balances write latency vs. throughput.
func ConsumeRingBuf(ctx context.Context, m *ebpf.Map, store *db.Store) error {
	rd, err := ringbuf.NewReader(m)
	if err != nil {
		return fmt.Errorf("open ring buffer reader: %w", err)
	}

	// Close the reader when ctx is done to unblock the Read() call.
	go func() {
		<-ctx.Done()
		rd.Close()
	}()
	defer rd.Close()

	const batchSize = 500
	const flushInterval = 100 * time.Millisecond

	buf := make([]db.TrafficLog, 0, batchSize)
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	log.Printf("ringbuf: consumer started")

	flush := func() {
		if len(buf) == 0 {
			return
		}
		log.Printf("ringbuf: flushing %d events to DB", len(buf))
		if err := store.BatchInsert(context.Background(), buf); err != nil {
			log.Printf("ringbuf: BatchInsert error: %v", err)
		}
		buf = buf[:0]
	}

	for {
		// Non-blocking read attempt; fall through to flush on timeout.
		record, err := rd.Read()
		if err != nil {
			if errors.Is(err, ringbuf.ErrClosed) {
				flush()
				return nil
			}
			// Check for flush tick between errors.
			select {
			case <-ticker.C:
				flush()
			default:
			}
			continue
		}

		var ev packetEvent
		if err := binary.Read(bytes.NewReader(record.RawSample), binary.LittleEndian, &ev); err != nil {
			continue
		}

		buf = append(buf, toTrafficLog(ev))

		if len(buf) >= batchSize {
			flush()
		} else {
			select {
			case <-ticker.C:
				flush()
			default:
			}
		}
	}
}
