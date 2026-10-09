// Copy into the pinned public plugin's internal/basispoints directory for
// offline contract generation. This test never receives real credentials.
package basispoints

import (
    "encoding/json"
    "os"
    "testing"
)

func TestFenjueExportContract(t *testing.T) {
    inputPath, outputPath := os.Getenv("FENJUE_CONTRACT_INPUT"), os.Getenv("FENJUE_CONTRACT_OUTPUT")
    if inputPath == "" || outputPath == "" { t.Skip("explicit fixture paths required") }
    raw, err := os.ReadFile(inputPath)
    if err != nil { t.Fatal(err) }
    var source map[string]any
    if err := json.Unmarshal(raw, &source); err != nil { t.Fatal(err) }
    model, ok := source["model"].(string)
    if !ok || model == "" { t.Fatal("explicit model required") }
    svc := NewService()
    cfg := DefaultConfig()
    cfg.ToolsVersionID = os.Getenv("FENJUE_TOOLS_VERSION")
    cfg.Models = []string{model}
    cfg.ModelMappings = map[string]string{model:model}
    if err := svc.ApplyConfig(cfg); err != nil { t.Fatal(err) }
    svc.SetHost(func(method string, payload any, out any) error {
        t.Fatalf("fixture attempted host access: %s", method)
        return nil
    })
    stream, _ := source["stream"].(bool)
    body, cred, err := svc.prepareRequest(ExecutorRequest{
        Model:model, Payload:raw, Stream:stream,
        AuthMetadata:map[string]any{"access_token":"FENJUE_FIXTURE_TOKEN", "account_id":"FENJUE_FIXTURE_ACCOUNT"},
    })
    if err != nil { t.Fatal(err) }
    if body["model"] != model || body["model_selection"] != "explicit" || body["store"] != false {
        t.Fatal("unexpected outbound contract")
    }
    data, err := json.MarshalIndent(map[string]any{
        "url":DefaultResponsesURL, "headers":authHeaders(cred,stream), "body":body,
        "protocol_version":Version,
    }, "", "  ")
    if err != nil { t.Fatal(err) }
    if err := os.WriteFile(outputPath,data,0600); err != nil { t.Fatal(err) }
}
