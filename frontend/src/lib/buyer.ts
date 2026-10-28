/**
 * 演示身份。
 *
 * 这个项目是单机演示：没有登录页，买家身份由前端声明。
 * 把默认值收敛到这里，避免同一个字符串散落在多个文件里，
 * 也方便以后接真实登录时只改一处。
 */
export const DEMO_BUYER_ID = "buyer-demo";

/** 历史项目遗留的买家标识，命中时视为缓存失效，重新初始化。 */
export const LEGACY_BUYER_IDS = ["pao-coder"];

/** 侧栏展示用的名字；配了 VITE_BUYER_ID 就显示配置值。 */
export function displayBuyerName(configured?: string): string {
  return configured || "演示用户";
}
