export interface InputBudget {
  limit_chars: number;
  required_chars: number;
  suggested_chars: number | null;
  maximum_chars: number;
  stage: string;
  request_id?: string;
}

export interface InputBudgetApproval {
  confirmed: true;
  limit_chars: number;
}

export function confirmInputBudget(
  budget: InputBudget,
  name: string,
): InputBudgetApproval | undefined {
  const count = (value: number) => value.toLocaleString("zh-CN");
  const explanation = `${name} · ${budget.stage}\n\n单次输入长度超限，超限输入尚未发送。\n当前上限：${count(budget.limit_chars)} 字符\n至少需要：${count(budget.required_chars)} 字符`;
  if (budget.suggested_chars === null) {
    window.alert(
      `${explanation}\n\n已超过可临时提高的范围，请缩短选中内容或拆分后重试。已完成结果保留。`,
    );
    return;
  }
  if (
    !window.confirm(
      `${explanation}\n申请临时上限：${count(budget.suggested_chars)} 字符\n\n这是输入长度限制；提高后可能增加模型费用。仅本次继续处理有效，结束、暂停或服务重启后恢复原上限。已完成批次会复用，全局设置保持原值。\n\n允许本次提高上限并继续？取消后保留结果，可稍后重试。`,
    )
  )
    return;
  return { confirmed: true, limit_chars: budget.suggested_chars };
}
