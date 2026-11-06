package heartbeat

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sipeed/picoclaw/pkg/tools"
)

func dueTestService(t *testing.T) *HeartbeatService {
	t.Helper()
	workspace := t.TempDir()
	if err := os.WriteFile(filepath.Join(workspace, "HEARTBEAT.md"), []byte("检查既有活动"), 0o600); err != nil {
		t.Fatal(err)
	}
	return NewHeartbeatService(workspace, 30, true)
}

func TestDueCheckerOnlyDispatchesWhenDueAndPreservesRegularInterval(t *testing.T) {
	hs := dueTestService(t)
	hs.stopChan = make(chan struct{})
	var due bool
	checks, calls := 0, 0
	hs.SetDueChecker(5*time.Second, func() bool { checks++; return due })
	hs.SetHandler(func(prompt, _, _ string) *tools.ToolResult {
		calls++
		if !strings.Contains(prompt, "检查既有活动") {
			t.Fatal("fast opportunity did not reuse the regular heartbeat prompt")
		}
		return tools.SilentResult("安静结束")
	})
	hs.checkDue()
	if checks != 1 || calls != 0 {
		t.Fatalf("idle check invoked handler: checks=%d calls=%d", checks, calls)
	}
	due = true
	hs.checkDue()
	if checks != 2 || calls != 1 || hs.interval != 30*time.Minute {
		t.Fatalf("due dispatch changed regular cadence: checks=%d calls=%d interval=%s", checks, calls, hs.interval)
	}
	hs.Stop()
	hs.checkDue()
	if checks != 2 || calls != 1 {
		t.Fatal("stopped service still checked or dispatched a due opportunity")
	}
}

func TestDueTickerUsesExistingHandlerBeforeRegularHeartbeat(t *testing.T) {
	hs := dueTestService(t)
	var checks atomic.Int32
	var delivered atomic.Bool
	called := make(chan struct{}, 1)
	hs.SetDueChecker(10*time.Millisecond, func() bool {
		checks.Add(1)
		return delivered.CompareAndSwap(false, true)
	})
	hs.SetHandler(func(string, string, string) *tools.ToolResult {
		called <- struct{}{}
		return tools.SilentResult("完成一次机会")
	})
	if err := hs.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(hs.Stop)
	select {
	case <-called:
	case <-time.After(500 * time.Millisecond):
		t.Fatal("due signal waited for the initial or regular heartbeat")
	}
	hs.Stop()
	previous := checks.Load()
	time.Sleep(40 * time.Millisecond)
	if checks.Load() != previous {
		t.Fatal("due checker continued polling after Stop")
	}
	select {
	case <-called:
		t.Fatal("one due signal invoked the shared handler twice")
	default:
	}
}

func TestFastAndRegularDispatchDoNotOverlap(t *testing.T) {
	hs := dueTestService(t)
	hs.stopChan = make(chan struct{})
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	t.Cleanup(func() { releaseOnce.Do(func() { close(release) }) })
	var calls, checks atomic.Int32
	hs.SetDueChecker(time.Second, func() bool { checks.Add(1); return true })
	hs.SetHandler(func(string, string, string) *tools.ToolResult {
		calls.Add(1)
		close(entered)
		<-release
		return tools.SilentResult("保存实际结果")
	})
	go func() { hs.executeHeartbeat(); close(done) }()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("regular heartbeat did not enter handler")
	}
	hs.checkDue()
	hs.executeHeartbeat()
	if calls.Load() != 1 || checks.Load() != 0 {
		t.Fatal("fast and regular triggers overlapped an active handler")
	}
	releaseOnce.Do(func() { close(release) })
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not complete after release")
	}
}
