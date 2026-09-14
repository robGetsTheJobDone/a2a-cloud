package a2apack

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type mathAgent struct{}

func (mathAgent) Definition() AgentDefinition {
	return AgentDefinition{Name: "math-go", Description: "Math helper", Version: "0.1.0"}
}

func (mathAgent) Sum(_ context.Context, request SumRequest) (SumResponse, error) {
	return SumResponse{Value: request.Left + request.Right}, nil
}

func TestCompileAgentWritesDSL(t *testing.T) {
	dir := t.TempDir()
	old, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := os.Chdir(old); err != nil {
			t.Fatal(err)
		}
	}()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	if err := CompileAgent(mathAgent{}); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(filepath.Join(".a2a", "agent.dsl.json"))
	if err != nil {
		t.Fatal(err)
	}
	var dsl map[string]any
	if err := json.Unmarshal(body, &dsl); err != nil {
		t.Fatal(err)
	}
	if dsl["language"] != "go" {
		t.Fatalf("language = %v", dsl["language"])
	}
	if dsl["name"] != "math-go" {
		t.Fatalf("name = %v", dsl["name"])
	}
}

func TestServeAgentInvokesMethod(t *testing.T) {
	rec := httptest.NewRecorder()
	req, err := http.NewRequest(
		http.MethodPost,
		"/_a2a/invoke/sum",
		strings.NewReader(`{"arguments":{"left":4,"right":5}}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	invokeSum(rec, req, mathAgent{})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != `{"result":{"value":9}}`+"\n" {
		t.Fatalf("body = %q", rec.Body.String())
	}
}
