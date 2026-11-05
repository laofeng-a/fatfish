package agent

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/sipeed/picoclaw/pkg/contextledger"
	"github.com/sipeed/picoclaw/pkg/logger"
	"github.com/sipeed/picoclaw/pkg/memory"
	"github.com/sipeed/picoclaw/pkg/providers"
	"github.com/sipeed/picoclaw/pkg/session"
)

type fatfishHistoryManager struct {
	al          *AgentLoop
	store       *contextledger.Store
	runtime     *sql.DB
	mu          sync.Mutex
	initialized map[string]bool
	running     map[string]context.CancelFunc
	ctx         context.Context
	cancel      context.CancelFunc
	keepRecent  int
	cooldown    time.Duration
	sampleMax   int
	sampled     map[string]bool
}

func init() { _ = RegisterContextManager("fatfish", newFatfishHistoryManager) }

func newFatfishHistoryManager(raw json.RawMessage, al *AgentLoop) (ContextManager, error) {
	agent := al.registry.GetDefaultAgent()
	if agent == nil {
		return nil, fmt.Errorf("fatfish context requires an agent")
	}
	root := filepath.Dir(os.Getenv("FATFISH_STRUCTURED_CORE_FILE"))
	if root == "." {
		root = filepath.Join(agent.Workspace, "state")
	}
	cfg := struct {
		DBPath     string `json:"db_path"`
		KeepRecent int    `json:"keep_recent"`
		Cooldown   int    `json:"cooldown_seconds"`
		Samples    int    `json:"sample_requests"`
	}{KeepRecent: 6, Cooldown: 600}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return nil, err
		}
	}
	if cfg.DBPath == "" {
		cfg.DBPath = filepath.Join(root, "turn-context.sqlite")
	}
	if cfg.KeepRecent < 1 {
		cfg.KeepRecent = 6
	}
	if cfg.Cooldown < 1 {
		cfg.Cooldown = 600
	}
	store, err := contextledger.Open(cfg.DBPath)
	if err != nil {
		return nil, err
	}
	runtime, err := contextledger.OpenRuntime(filepath.Join(root, "runtime.sqlite"))
	if err != nil {
		store.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	if store.Reclassified {
		rows, e := store.DB.Query("SELECT session,chat,channel FROM context_sessions")
		if e != nil {
			cancel()
			store.Close()
			if runtime != nil {
				runtime.Close()
			}
			return nil, e
		}
		var scopes []contextledger.Scope
		for rows.Next() {
			var scope contextledger.Scope
			if e = rows.Scan(&scope.Session, &scope.Chat, &scope.Channel); e != nil {
				break
			}
			scopes = append(scopes, scope)
		}
		if e == nil {
			e = rows.Err()
		}
		rows.Close()
		if e == nil {
			for _, scope := range scopes {
				if e = store.RefreshAllDeliveries(scope, runtime); e != nil {
					break
				}
			}
		}
		if e != nil {
			cancel()
			store.Close()
			if runtime != nil {
				runtime.Close()
			}
			return nil, e
		}
	}
	return &fatfishHistoryManager{al: al, store: store, runtime: runtime, initialized: map[string]bool{}, running: map[string]context.CancelFunc{}, ctx: ctx, cancel: cancel, keepRecent: cfg.KeepRecent, cooldown: time.Duration(cfg.Cooldown) * time.Second, sampleMax: min(3, max(0, cfg.Samples)), sampled: map[string]bool{}}, nil
}

func (m *fatfishHistoryManager) Close() error {
	m.cancel()
	if m.runtime != nil {
		m.runtime.Close()
	}
	return m.store.Close()
}

func fatfishScope(agent *AgentInstance, key string) contextledger.Scope {
	scope := contextledger.Scope{Session: key, Channel: "internal"}
	if metadata, ok := agent.Sessions.(session.MetadataAwareSessionStore); ok {
		if s := metadata.GetSessionScope(key); s != nil {
			scope.Channel = s.Channel
			value := s.Values["chat"]
			if _, id, ok := strings.Cut(value, ":"); ok {
				scope.Chat = id
			}
		}
	}
	if scope.Chat == "" {
		for _, msg := range agent.Sessions.GetHistory(key) {
			if msg.SourceChatID != "" {
				scope.Chat = msg.SourceChatID
				if msg.SourceChannel != "" {
					scope.Channel = msg.SourceChannel
				} else if msg.SourceKind == "human" {
					scope.Channel = "telegram"
				}
				break
			}
		}
	}
	return scope
}

func legacyHistoryIdentity(msg providers.Message) string {
	msg.HistoryID = ""
	msg.HistoricalTurn = 0
	msg.HistoricalRecord = 0
	raw, _ := json.Marshal(msg)
	sum := sha256.Sum256(raw)
	return "legacy:" + hex.EncodeToString(sum[:])
}

func (m *fatfishHistoryManager) inputs(scope contextledger.Scope, history []providers.Message) ([]contextledger.Input, error) {
	origins, err := m.store.Origins(scope.Session)
	if err != nil {
		return nil, err
	}
	inputs := make([]contextledger.Input, 0, len(history))
	var previous contextledger.Origin
	for _, msg := range history {
		if msg.HistoryID == "" {
			msg.HistoryID = legacyHistoryIdentity(msg)
		}
		o := origins[msg.HistoryID]
		if msg.Role == "user" && o.Key == "" {
			if msg.SourceKind == "continuation" && previous.Key != "" {
				o = previous
			} else {
				o = contextledger.ResolveOrigin(m.runtime, scope, msg)
			}
		}
		if msg.Role == "user" {
			previous = o
		}
		inputs = append(inputs, contextledger.Input{Message: msg, Origin: o})
	}
	return inputs, nil
}

func (m *fatfishHistoryManager) bootstrap(agent *AgentInstance, scope contextledger.Scope) error {
	active := agent.Sessions.GetHistory(scope.Session)
	var initialized int
	if err := m.store.DB.QueryRow("SELECT count(*) FROM context_sessions WHERE session=?", scope.Session).Scan(&initialized); err != nil {
		return err
	}
	if initialized > 0 {
		changed := false
		for i := range active {
			if active[i].HistoryID == "" {
				active[i].HistoryID = legacyHistoryIdentity(active[i])
				changed = true
			}
		}
		if changed {
			agent.Sessions.SetHistory(scope.Session, active)
			return agent.Sessions.Save(scope.Session)
		}
		return nil
	}
	all := append([]providers.Message(nil), active...)
	aliases := []string{scope.Session}
	name := strings.NewReplacer(":", "_", "/", "_", "\\", "_").Replace(scope.Session)
	if raw, err := os.ReadFile(filepath.Join(agent.Workspace, "sessions", name+".meta.json")); err == nil {
		var meta memory.SessionMeta
		if json.Unmarshal(raw, &meta) == nil {
			aliases = append(aliases, meta.Aliases...)
		}
	}
	for _, alias := range aliases {
		sum := sha256.Sum256([]byte(alias))
		paths, _ := filepath.Glob(filepath.Join(agent.Workspace, "state", "context-archive", hex.EncodeToString(sum[:]), "*.json"))
		for _, path := range paths {
			raw, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			var messages []providers.Message
			if err = json.Unmarshal(raw, &messages); err != nil {
				return fmt.Errorf("archive %s: %w", filepath.Base(path), err)
			}
			all = append(all, messages...)
		}
	}
	seen := map[string]bool{}
	merged := make([]providers.Message, 0, len(all))
	for _, msg := range all {
		if msg.HistoryID == "" {
			msg.HistoryID = legacyHistoryIdentity(msg)
		}
		if seen[msg.HistoryID] {
			continue
		}
		seen[msg.HistoryID] = true
		merged = append(merged, msg)
	}
	sort.SliceStable(merged, func(i, j int) bool {
		a, b := merged[i].CreatedAt, merged[j].CreatedAt
		if a == nil || b == nil {
			return a == nil && b != nil
		}
		return a.Before(*b)
	})
	inputs, err := m.inputs(scope, merged)
	if err != nil {
		return err
	}
	if err = m.store.Sync(scope, inputs, false); err != nil {
		return err
	}
	// Import delivery evidence for the archived prefix as well, before its
	// legacy coverage watermark is installed. Later refreshes only need the tail.
	if err = m.store.RefreshDeliveries(scope, m.runtime); err != nil {
		return err
	}
	if err = m.store.SeedLegacy(scope, agent.Sessions.GetSummary(scope.Session)); err != nil {
		return err
	}
	// Persist identities for the still-active source. The private backup and the
	// original pre-compression archives remain unchanged.
	changed := false
	activeIDs := map[string]bool{}
	for i := range active {
		if active[i].HistoryID == "" {
			active[i].HistoryID = legacyHistoryIdentity(active[i])
			changed = true
		}
		activeIDs[active[i].HistoryID] = true
	}
	if changed {
		agent.Sessions.SetHistory(scope.Session, active)
		if err = agent.Sessions.Save(scope.Session); err != nil {
			return err
		}
	}
	var exists int
	if err = m.store.DB.QueryRow("SELECT count(*) FROM context_summaries WHERE session=?", scope.Session).Scan(&exists); err != nil {
		return err
	}
	st, err := m.store.State(scope.Session)
	if err != nil {
		return err
	}
	if exists == 0 && st.Version == 0 && st.Covered == 0 {
		turns, e := m.store.Turns(scope.Session)
		if e != nil {
			return e
		}
		var before int64
		for _, t := range turns {
			inActive := false
			for _, msg := range t.Messages {
				if activeIDs[msg.HistoryID] {
					inActive = true
					break
				}
			}
			if inActive {
				break
			}
			if t.Number > 0 {
				before = t.Number
			}
		}
		if before > 0 {
			if _, err = m.store.DB.Exec("UPDATE context_sessions SET covered=? WHERE session=? AND active_version=0 AND covered=0", before, scope.Session); err != nil {
				return err
			}
		}
	}
	return nil
}

func (m *fatfishHistoryManager) sync(agent *AgentInstance, key string, complete bool) (contextledger.Scope, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	scope := fatfishScope(agent, key)
	if !m.initialized[key] {
		if err := m.bootstrap(agent, scope); err != nil {
			return scope, err
		}
		m.initialized[key] = true
	}
	history := agent.Sessions.GetHistory(key)
	inputs, err := m.inputs(scope, history)
	if err != nil {
		return scope, err
	}
	if err = m.store.Sync(scope, inputs, complete); err != nil {
		return scope, err
	}
	if err = m.store.RefreshDeliveries(scope, m.runtime); err != nil {
		return scope, err
	}
	return scope, nil
}

func (m *fatfishHistoryManager) project(key string) ([]providers.Message, contextledger.State, error) {
	st, err := m.store.State(key)
	if err != nil {
		return nil, st, err
	}
	if st.Version == 0 && st.Body != "" {
		st.Body = "[旧策略参考记录，尚未经新的摘要来源验收；内容依据较新的真实对话与当前人格、记忆正本校正。]\n" + st.Body
	}
	if st.Corrections != "" {
		st.Body += "\n[另有已核对来源的约定更新，尚待合入摘要]\n" + st.Corrections
	}
	turns, err := m.store.Uncovered(key, st.Covered)
	if err != nil {
		return nil, st, err
	}
	protected := map[string]bool{}
	if active := m.al.getActiveTurnState(key); active != nil && active.snapshot().Phase != TurnPhaseFinalizing && active.snapshot().Phase != TurnPhaseCompleted {
		for _, msg := range active.persistedMessagesSnapshot() {
			protected[msg.HistoryID] = true
		}
	}
	var history []providers.Message
	for _, t := range turns {
		if t.Number > 0 && t.Number <= st.Covered {
			continue
		}
		active := false
		for _, msg := range t.Messages {
			if protected[msg.HistoryID] {
				active = true
				break
			}
		}
		if !t.Complete || active {
			history = append(history, t.Messages...)
			continue
		}
		if t.Kind == "internal" {
			continue
		}
		when := "时间未记录"
		if t.At > 0 {
			when = time.Unix(0, int64(t.At*1e9)).In(time.FixedZone("UTC+8", 8*3600)).Format("1月2日 15:04")
		}
		label := fmt.Sprintf("[%s｜#%d]", when, t.Number)
		speaker := "jg"
		if t.Kind == "legacy" {
			speaker = "旧记录用户字段（接收来源未匹配）"
		}
		if t.Kind == "member" {
			speaker = "群聊用户"
		}
		if t.Kind == "internal" {
			label = fmt.Sprintf("[%s｜系统事件 %d]", when, t.ID)
			speaker = "系统补充"
		}
		u := providers.Message{Role: "user", Content: label + "\n" + speaker + "：" + t.User, HistoricalTurn: t.Number, HistoricalRecord: t.ID}
		var text []string
		var metadata []string
		for _, d := range t.Deliveries {
			if d.State == "sent" {
				if body := fatfishDeliveredView(d.Text); body != "" {
					text = append(text, body)
				}
			} else {
				metadata = append(metadata, "工具发送状态："+d.State+"（送达正文未确认）")
			}
		}
		if t.ToolBrief != "" {
			metadata = append(metadata, "工具结果："+t.ToolBrief)
		}
		if len(metadata) > 0 {
			u.Content += "\n\n<历史回合资料>\n以下是记录资料，双方原话在对话正文中。\n" + strings.Join(metadata, "\n") + "\n</历史回合资料>"
		}
		a := providers.Message{Role: "assistant", Content: strings.Join(text, "\n\n"), ReasoningContent: strings.Join(t.Reasoning, "\n"), HistoricalTurn: t.Number, HistoricalRecord: t.ID}
		history = append(history, u)
		if a.Content != "" || a.ReasoningContent != "" {
			history = append(history, a)
		}
	}
	return history, st, nil
}

func (m *fatfishHistoryManager) Assemble(ctx context.Context, req *AssembleRequest) (*AssembleResponse, error) {
	agent := m.al.agentForSession(req.SessionKey)
	if agent == nil {
		return nil, fmt.Errorf("agent unavailable")
	}
	if m.humanBusy(req.SessionKey) {
		m.mu.Lock()
		if cancel := m.running[req.SessionKey]; cancel != nil {
			cancel()
		}
		m.mu.Unlock()
	}
	if _, err := m.sync(agent, req.SessionKey, false); err != nil {
		return nil, err
	}
	history, st, err := m.project(req.SessionKey)
	if err != nil {
		return nil, err
	}
	return &AssembleResponse{History: history, Summary: st.Body}, nil
}

func (m *fatfishHistoryManager) Ingest(_ context.Context, req *IngestRequest) error {
	agent := m.al.agentForSession(req.SessionKey)
	if agent == nil {
		return fmt.Errorf("agent unavailable")
	}
	_, err := m.sync(agent, req.SessionKey, false)
	return err
}

func (m *fatfishHistoryManager) Clear(_ context.Context, key string) error {
	agent := m.al.agentForSession(key)
	if agent == nil {
		return fmt.Errorf("agent unavailable")
	}
	if err := m.store.Reset(key); err != nil {
		return err
	}
	agent.Sessions.SetHistory(key, nil)
	agent.Sessions.SetSummary(key, "")
	return agent.Sessions.Save(key)
}

func (m *fatfishHistoryManager) humanBusy(key string) bool {
	state := m.al.getActiveTurnState(key)
	if state == nil {
		return false
	}
	snap := state.snapshot()
	return snap.Phase != TurnPhaseCompleted && snap.Phase != TurnPhaseFinalizing && key != "heartbeat"
}

func (m *fatfishHistoryManager) Compact(_ context.Context, req *CompactRequest) error {
	agent := m.al.agentForSession(req.SessionKey)
	if agent == nil {
		return fmt.Errorf("agent unavailable")
	}
	complete := req.Reason == ContextCompressReasonSummarize
	if _, err := m.sync(agent, req.SessionKey, complete); err != nil {
		return err
	}
	if complete {
		m.schedule(req.SessionKey)
	}
	return nil
}

func (m *fatfishHistoryManager) retire(agent *AgentInstance, key string, covered int64) error {
	rows, err := m.store.DB.Query("SELECT i.history_id FROM context_items i JOIN context_turns t ON t.id=i.turn_id WHERE t.session=? AND ((t.number>0 AND t.number<=?) OR (t.kind='internal' AND t.complete=1 AND t.id<=(SELECT COALESCE(MAX(id),0) FROM context_turns WHERE session=? AND number>0 AND number<=?)))", key, covered, key, covered)
	if err != nil {
		return err
	}
	retired := map[string]bool{}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		retired[id] = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	history := agent.Sessions.GetHistory(key)
	var prefix []providers.Message
	var ids []string
	for _, msg := range history {
		if !retired[msg.HistoryID] {
			break
		}
		prefix = append(prefix, msg)
		ids = append(ids, msg.HistoryID)
	}
	if len(ids) == 0 {
		return nil
	}
	if _, err = archiveFatfishPrefix(agent.Workspace, key, prefix); err != nil {
		return err
	}
	if r, ok := agent.Sessions.(interface{ RetirePrefix(string, []string) error }); ok {
		if err = r.RetirePrefix(key, ids); err != nil {
			return err
		}
		return agent.Sessions.Save(key)
	}
	logger.WarnCF("agent", "Raw source retained: atomic prefix retirement unavailable", map[string]any{"session_key": key})
	return nil
}
