Execute one Jarvis plan node as CoderNodeExecuteRuntime.

{{ temporal_context }}

User objective: {{ user_objective }}
Node id: {{ node_id }}
Node objective: {{ node_objective }}
Expected output: {{ expected_output }}
{{#resolved_inputs_section}}

Resolved inputs:
{{ resolved_inputs_section }}
{{/resolved_inputs_section}}
{{#additional_instructions_section}}

Additional instructions:
{{ additional_instructions_section }}
{{/additional_instructions_section}}

Return a concise result suitable for a NodeResult summary.
Do not ask for routine confirmation. Respect permission limits and request approval only when required.
