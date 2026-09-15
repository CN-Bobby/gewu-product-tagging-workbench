// Which draft-stream events the workbench itself owns, and which belong to the legacy chat
// projection that shares the same stream.
//
// The draft stream carries both: table/draft/batch/proposal state that every panel needs, and the
// legacy conversation's messages and questions. Keeping the classification here — rather than
// implied by one long callback in main.jsx — is what makes the cutover testable, and it is the
// single place that says which events are the old orchestration's business.
//
// Pure module on purpose: no React, no DOM, no fetch.

// Projections of the legacy conversation: they only describe what the old chat panel shows.
export const LEGACY_CHAT_KINDS=['chat_chunk','message_presentation','message','question'];

// Events that grow a collection on the loaded worksheet.
export const COLLECTION_BY_KIND={column:'columns',proposal:'proposals',proposal_group:'proposals',
  job:'jobs',version:'versions',message:'messages',question:'questions'};

export const isLegacyChatKind=kind=>LEGACY_CHAT_KINDS.includes(kind);

// "workbench" is the worksheet and its execution state; "legacy_chat" is the old orchestration's
// projection. Nothing is dropped either way — the classification exists so a cutover can be
// asserted, not so events can be filtered away.
export const draftEventOwner=kind=>isLegacyChatKind(kind)?'legacy_chat':'workbench';

export function mergeById(items,item){
  return items.some(existing=>existing.id===item.id)
    ?items.map(existing=>existing.id===item.id?{...existing,...item}:existing)
    :[...items,item];
}

export function applyDraftEvent(data,kind,payload){
  if(!data)return data;
  if(kind==='text_record')
    return {...data,rows:data.rows.map(row=>row.id===payload.product_id?{...row,text_record:payload}:row)};
  if(kind==='cell')
    return {...data,rows:data.rows.map(row=>row.id===payload.product_id
      ?{...row,cells:{...row.cells,[payload.column_id]:payload}}:row)};
  if(kind==='chat_chunk')
    return {...data,messages:data.messages.map(message=>message.id===payload.id
      ?{...message,text:message.text+payload.text}:message)};
  if(kind==='message_presentation')
    return {...data,messages:data.messages.map(message=>message.id===payload.id
      ?{...message,presentation:payload.presentation,error_code:payload.error_code||message.error_code}:message)};
  const collection=COLLECTION_BY_KIND[kind];
  return collection?{...data,[collection]:mergeById(data[collection]||[],payload)}:data;
}
