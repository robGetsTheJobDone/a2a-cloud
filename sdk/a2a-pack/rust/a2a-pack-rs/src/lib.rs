use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

/// The whole contract this SDK supports: one skill, named `sum`.
///
/// This crate is a demo of the sidecar worker protocol, not a general agent
/// runtime. [`serve_agent`] answers only `POST /_a2a/invoke/sum` and 404s
/// everything else, and `agent_dsl` always compiles a single `sum` skill, so a
/// second method added here would be unreachable and unadvertised. Use the
/// Python or TypeScript SDK to build an agent with more than one tool.
pub trait A2AAgent {
    fn definition(&self) -> AgentDefinition;
    fn sum(&self, request: SumRequest) -> Result<SumResponse, String>;
}

pub struct AgentDefinition {
    pub name: &'static str,
    pub description: &'static str,
    pub version: &'static str,
}

pub struct SumRequest {
    pub left: f64,
    pub right: f64,
}

pub struct SumResponse {
    pub value: f64,
}

pub fn compile_agent(agent: &impl A2AAgent) -> Result<(), String> {
    fs::create_dir_all(".a2a").map_err(|error| error.to_string())?;
    let dsl = agent_dsl(agent.definition());
    fs::write(".a2a/agent.dsl.json", format!("{dsl}\n")).map_err(|error| error.to_string())?;
    println!("compiled .a2a/agent.dsl.json");
    Ok(())
}

pub fn serve_agent(agent: impl A2AAgent) -> Result<(), String> {
    let host = env::var("A2A_WORKER_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port = env::var("A2A_WORKER_PORT").unwrap_or_else(|_| "9001".to_string());
    let listener =
        TcpListener::bind(format!("{host}:{port}")).map_err(|error| error.to_string())?;
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => handle_stream(stream, &agent),
            Err(error) => eprintln!("{error}"),
        }
    }
    Ok(())
}

fn handle_stream(mut stream: TcpStream, agent: &impl A2AAgent) {
    let mut buffer = [0_u8; 8192];
    let Ok(size) = stream.read(&mut buffer) else {
        return;
    };
    let request = String::from_utf8_lossy(&buffer[..size]);
    if !request.starts_with("POST /_a2a/invoke/sum ") {
        write_response(&mut stream, 404, r#"{"error":"not found"}"#);
        return;
    }
    let Some(left) = number_arg(&request, "left") else {
        write_response(&mut stream, 400, r#"{"error":"left is required"}"#);
        return;
    };
    let Some(right) = number_arg(&request, "right") else {
        write_response(&mut stream, 400, r#"{"error":"right is required"}"#);
        return;
    };
    match agent.sum(SumRequest { left, right }) {
        Ok(response) => {
            let body = format!(r#"{{"result":{{"value":{}}}}}"#, response.value);
            write_response(&mut stream, 200, &body);
        }
        Err(error) => {
            let body = format!(r#"{{"error":"{}"}}"#, json_escape(&error));
            write_response(&mut stream, 500, &body);
        }
    }
}

fn agent_dsl(def: AgentDefinition) -> String {
    let version = if def.version.is_empty() {
        "0.1.0"
    } else {
        def.version
    };
    format!(
        r#"{{
  "schema_version": "2026-06-04",
  "language": "rust",
  "name": "{}",
  "description": "{}",
  "version": "{}",
  "entrypoint": {{
    "module": null,
    "class_name": null,
    "function": null,
    "command": ["./worker"]
  }},
  "skills": [
    {{
      "name": "sum",
      "description": "Add two numbers",
      "handler": "sum",
      "tags": [],
      "scopes": [],
      "stream": false,
      "policy": {{
        "timeout_seconds": 30,
        "idempotent": true
      }},
      "input_schema": {{
        "type": "object",
        "properties": {{
          "left": {{ "type": "number" }},
          "right": {{ "type": "number" }}
        }},
        "required": ["left", "right"],
        "additionalProperties": false
      }},
      "output_schema": {{
        "type": "object",
        "properties": {{
          "value": {{ "type": "number" }}
        }},
        "required": ["value"],
        "additionalProperties": false
      }}
    }}
  ],
  "capabilities": {{}},
  "input_modes": ["application/json"],
  "output_modes": ["application/json"],
  "required_secrets": [],
  "required_env": [],
  "consumer_setup": {{ "fields": [] }},
  "runtime": {{ "sandbox": "microsandbox" }},
  "template_lineage": null,
  "meta_agent_manifest": null,
  "state_schema": null,
  "workspace_access": {{ "enabled": false }},
  "config_schema": null,
  "auth": {{
    "model": "NoAuth",
    "strategy": "public",
    "principal_schema": {{
      "type": "object",
      "properties": {{}},
      "additionalProperties": false
    }},
    "required": false
  }},
  "metadata": {{}}
}}"#,
        json_escape(def.name),
        json_escape(def.description),
        json_escape(version)
    )
}

fn write_response(stream: &mut TcpStream, status: u16, body: &str) {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "Error",
    };
    let response = format!(
        "HTTP/1.1 {status} {reason}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
}

fn number_arg(body: &str, key: &str) -> Option<f64> {
    let marker = format!("\"{key}\"");
    let start = body.find(&marker)?;
    let after_key = &body[start + marker.len()..];
    let colon = after_key.find(':')?;
    let value = after_key[colon + 1..].trim_start();
    let end = value
        .find(|ch: char| !matches!(ch, '0'..='9' | '.' | '-' | '+' | 'e' | 'E'))
        .unwrap_or(value.len());
    value[..end].parse().ok()
}

fn json_escape(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if ch.is_control() => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => out.push(ch),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MathAgent;

    impl A2AAgent for MathAgent {
        fn definition(&self) -> AgentDefinition {
            AgentDefinition {
                name: "math-rust",
                description: "Math helper",
                version: "0.1.0",
            }
        }

        fn sum(&self, request: SumRequest) -> Result<SumResponse, String> {
            Ok(SumResponse {
                value: request.left + request.right,
            })
        }
    }

    #[test]
    fn dsl_contains_agent_contract() {
        let dsl = agent_dsl(MathAgent.definition());
        assert!(dsl.contains(r#""language": "rust""#));
        assert!(dsl.contains(r#""name": "math-rust""#));
        assert!(dsl.contains(r#""command": ["./worker"]"#));
    }

    #[test]
    fn implemented_trait_method_runs() {
        let response = MathAgent
            .sum(SumRequest {
                left: 4.0,
                right: 5.0,
            })
            .expect("sum should succeed");
        assert_eq!(response.value, 9.0);
    }
}
