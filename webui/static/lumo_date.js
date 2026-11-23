/* 统一前端日期和时间展示，保留数据原值供接口比较。 */
(() => {
  "use strict";

  const isoDate = /^(\d{4})-(\d{2})-(\d{2})$/;
  const isoDateTime = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;
  const compactDate = /^(\d{4})(\d{2})(\d{2})$/;

  function format(value) {
    if (value == null || value === "") return "";
    if (value instanceof Date) return formatEpoch(value.getTime());

    const text = String(value).trim();
    if (!text) return "";

    let match = text.match(isoDateTime);
    if (match) {
      if (match[7] && match[7].toUpperCase() !== "Z" && !/^\+08:?00$/.test(match[7])) return formatEpoch(Date.parse(text));
      if (match[7]?.toUpperCase() === "Z") return formatEpoch(Date.parse(text));
      return `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}${match[6] ? `:${match[6]}` : ""}`;
    }
    match = text.match(isoDate);
    if (match) return `${match[1]}-${match[2]}-${match[3]}`;
    match = text.match(compactDate);
    if (match) return `${match[1]}-${match[2]}-${match[3]}`;
    return text;
  }

  function date(value) {
    const text = format(value);
    return text ? text.slice(0, 10) : "";
  }

  function formatEpoch(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return format(value);
    const dateValue = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
    if (Number.isNaN(dateValue.getTime())) return format(value);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      hour12: false,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).formatToParts(dateValue).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }

  function formatText(value) {
    if (value == null || value === "") return "";
    return String(value).replace(
      /(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)/g,
      match => format(match),
    );
  }

  window.LumoDate = {date, format, formatEpoch, formatText};
})();
