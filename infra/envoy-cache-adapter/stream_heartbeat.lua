local ping = '\nevent: ping\ndata: {"type":"ping"}\n\n'

function heartbeat_transformer()
  local line_kind = "empty"
  local block_comment = false
  local block_field = false
  local previous_cr = false

  return function(bytes)
    local output = {}
    local copied_from = 1
    for offset = 1, #bytes do
      local character = string.byte(bytes, offset)
      if character == 10 and previous_cr then
        previous_cr = false
      elseif character == 10 or character == 13 then
        if line_kind == "empty" then
          if block_comment and not block_field then
            output[#output + 1] = string.sub(bytes, copied_from, offset)
            output[#output + 1] = ping
            copied_from = offset + 1
          end
          block_comment = false
          block_field = false
        elseif line_kind == "comment" then
          block_comment = true
        else
          block_field = true
        end
        line_kind = "empty"
        previous_cr = character == 13
      else
        if line_kind == "empty" then
          line_kind = character == 58 and "comment" or "field"
        end
        previous_cr = false
      end
    end
    if #output == 0 then
      return bytes
    end
    output[#output + 1] = string.sub(bytes, copied_from)
    return table.concat(output)
  end
end

function envoy_on_response(handle)
  local metadata = handle:streamInfo():dynamicMetadata():get("turnstile.request") or {}
  local headers = handle:headers()
  local content_type = string.lower(headers:get("content-type") or "")
  local media_type = string.match(content_type, "^%s*([^;%s]+)")
  local encoding = string.lower(headers:get("content-encoding") or "identity")
  if metadata.api_format ~= "anthropic_messages" or media_type ~= "text/event-stream"
      or headers:get(":status") ~= "200" or encoding ~= "identity" then
    return
  end
  for _, name in ipairs({"content-length", "etag", "content-md5", "digest", "content-digest"}) do
    headers:remove(name)
  end
  local transform = heartbeat_transformer()
  for chunk in handle:bodyChunks() do
    local bytes = chunk:getBytes(0, chunk:length())
    local converted = transform(bytes)
    if converted ~= bytes then
      chunk:setBytes(converted)
    end
  end
end