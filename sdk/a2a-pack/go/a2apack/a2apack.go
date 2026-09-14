package a2apack

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
)

// Agent is the whole contract this SDK supports: one skill, named "sum".
//
// This package is a demo of the sidecar worker protocol, not a general agent
// runtime. ServeAgent registers only "/_a2a/invoke/sum" and agentDSL always
// compiles a single "sum" skill, so a second method added here would be
// unreachable and unadvertised. Use the Python or TypeScript SDK to build an
// agent with more than one tool.
type Agent interface {
	Definition() AgentDefinition
	Sum(context.Context, SumRequest) (SumResponse, error)
}

type AgentDefinition struct {
	Name        string
	Description string
	Version     string
}

type SumRequest struct {
	Left  float64 `json:"left"`
	Right float64 `json:"right"`
}

type SumResponse struct {
	Value float64 `json:"value"`
}

type workerEnvelope struct {
	Arguments json.RawMessage `json:"arguments"`
}

func CompileAgent(agent Agent) error {
	if err := os.MkdirAll(".a2a", 0o755); err != nil {
		return err
	}
	body, err := json.MarshalIndent(agentDSL(agent.Definition()), "", "  ")
	if err != nil {
		return err
	}
	body = append(body, '\n')
	if err := os.WriteFile(".a2a/agent.dsl.json", body, 0o644); err != nil {
		return err
	}
	fmt.Println("compiled .a2a/agent.dsl.json")
	return nil
}

func ServeAgent(agent Agent) error {
	host := os.Getenv("A2A_WORKER_HOST")
	if host == "" {
		host = "127.0.0.1"
	}
	port := os.Getenv("A2A_WORKER_PORT")
	if port == "" {
		port = "9001"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/_a2a/invoke/sum", func(w http.ResponseWriter, r *http.Request) {
		invokeSum(w, r, agent)
	})
	return http.ListenAndServe(host+":"+port, mux)
}

func invokeSum(w http.ResponseWriter, r *http.Request, agent Agent) {
	if r.Method != http.MethodPost {
		http.NotFound(w, r)
		return
	}
	var envelope workerEnvelope
	if err := json.NewDecoder(r.Body).Decode(&envelope); err != nil {
		writeWorkerError(w, http.StatusBadRequest, err)
		return
	}
	var request SumRequest
	if len(envelope.Arguments) == 0 {
		writeWorkerError(w, http.StatusBadRequest, errors.New("arguments are required"))
		return
	}
	if err := json.Unmarshal(envelope.Arguments, &request); err != nil {
		writeWorkerError(w, http.StatusBadRequest, err)
		return
	}
	response, err := agent.Sum(r.Context(), request)
	if err != nil {
		writeWorkerError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"result": response})
}

func agentDSL(def AgentDefinition) map[string]any {
	version := def.Version
	if version == "" {
		version = "0.1.0"
	}
	return map[string]any{
		"schema_version": "2026-06-04",
		"language":       "go",
		"name":           def.Name,
		"description":    def.Description,
		"version":        version,
		"entrypoint": map[string]any{
			"module":     nil,
			"class_name": nil,
			"function":   nil,
			"command":    []string{"./worker"},
		},
		"skills": []map[string]any{
			{
				"name":        "sum",
				"description": "Add two numbers",
				"handler":     "sum",
				"tags":        []string{},
				"scopes":      []string{},
				"stream":      false,
				"policy": map[string]any{
					"timeout_seconds": 30,
					"idempotent":      true,
				},
				"input_schema": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"left":  map[string]any{"type": "number"},
						"right": map[string]any{"type": "number"},
					},
					"required":             []string{"left", "right"},
					"additionalProperties": false,
				},
				"output_schema": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"value": map[string]any{"type": "number"},
					},
					"required":             []string{"value"},
					"additionalProperties": false,
				},
			},
		},
		"capabilities":        map[string]any{},
		"input_modes":         []string{"application/json"},
		"output_modes":        []string{"application/json"},
		"required_secrets":    []string{},
		"required_env":        []string{},
		"consumer_setup":      map[string]any{"fields": []any{}},
		"runtime":             map[string]any{"sandbox": "microsandbox"},
		"workspace_access":    map[string]any{"enabled": false},
		"config_schema":       nil,
		"state_schema":        nil,
		"template_lineage":    nil,
		"meta_agent_manifest": nil,
		"auth": map[string]any{
			"model":            "NoAuth",
			"strategy":         "public",
			"principal_schema": map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false},
			"required":         false,
		},
		"metadata": map[string]any{},
	}
}

func writeWorkerError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]any{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("content-type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
