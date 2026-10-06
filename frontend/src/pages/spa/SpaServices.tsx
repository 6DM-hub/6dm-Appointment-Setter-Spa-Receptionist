import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  FileUp,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  createManagedService,
  bulkDeleteManagedServices,
  createServiceCategory,
  deleteServiceCategory,
  fetchManagedServices,
  fetchServiceCategories,
  getApiErrorMessage,
  updateManagedService,
  updateServiceCategory,
  type ManagedService,
  type ServiceCategory,
  type ServicePayload,
} from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { canEditSpaSettings } from "../../auth/roles";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

const emptyService = (categoryId = ""): ServicePayload => ({
  category_id: categoryId,
  name: "",
  description: "",
  price: "",
  duration_minutes: 60,
  is_active: true,
});

type ImportAction = "skip" | "import" | "create";
type ImportRow = {
  line: number;
  name: string;
  categoryName: string;
  categoryId: string;
  duration: number | null;
  price: string;
  description: string;
  isActive: boolean;
  error: string | null;
  duplicate: boolean;
  action: ImportAction;
};

const csvCell = (value: string): string =>
  value.trim().replace(/^"|"$/g, "").replace(/""/g, '"');

const parseCsv = (content: string): string[][] => {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < content.length; index += 1) {
    const character = content[index];
    if (character === '"') {
      if (quoted && content[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else quoted = !quoted;
    } else if (character === "," && !quoted) {
      row.push(cell);
      cell = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && content[index + 1] === "\n") index += 1;
      row.push(cell);
      if (row.some((value) => value.trim())) rows.push(row);
      row = [];
      cell = "";
    } else cell += character;
  }
  if (quoted) throw new Error("The CSV contains an unterminated quoted field");
  row.push(cell);
  if (row.some((value) => value.trim())) rows.push(row);
  return rows;
};

const normalizedHeader = (value: string): string =>
  value.toLowerCase().replace(/[^a-z0-9]/g, "");

const headerIndex = (headers: string[], names: string[]): number =>
  headers.findIndex((header) => names.includes(normalizedHeader(header)));

const duplicateKey = (
  row: Pick<ImportRow, "name" | "categoryId" | "duration" | "price">,
): string =>
  [
    row.name.trim().toLowerCase(),
    row.categoryId,
    row.duration,
    row.price.trim(),
  ].join("|");

const importStatus = (value: string): boolean =>
  !["inactive", "disabled", "off", "false"].includes(
    value.trim().toLowerCase(),
  );

const displayPrice = (value: string | null): string => {
  if (!value) return "—";
  return value.trim().startsWith("$") ? value.trim() : `$${value.trim()}`;
};

export default function SpaServices() {
  const { role } = useAuth();
  const editable = canEditSpaSettings(role);
  const [tab, setTab] = useState<"all" | "categories">("all");
  const [categories, setCategories] = useState<ServiceCategory[]>([]);
  const [services, setServices] = useState<ManagedService[]>([]);
  const [filter, setFilter] = useState("");
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<ServicePayload | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [categoryName, setCategoryName] = useState("");
  const [categoryEditId, setCategoryEditId] = useState<string | null>(null);
  const [categoryModal, setCategoryModal] = useState<"add" | "edit" | null>(
    null,
  );
  const [openCategoryMenu, setOpenCategoryMenu] = useState<string | null>(null);
  const [openServiceMenu, setOpenServiceMenu] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<{
    top: number;
    left: number;
  } | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleteTarget, setDeleteTarget] = useState<ManagedService[] | null>(
    null,
  );
  const [deleteAllOpen, setDeleteAllOpen] = useState(false);
  const [deleteAllText, setDeleteAllText] = useState("");
  const [success, setSuccess] = useState<string | null>(null);
  const serviceMenuRef = useRef<HTMLButtonElement | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importRows, setImportRows] = useState<ImportRow[]>([]);
  const [importError, setImportError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [nextCategories, nextServices] = await Promise.all([
        fetchServiceCategories(),
        fetchManagedServices(),
      ]);
      setCategories(nextCategories);
      setServices(nextServices);
      setError(null);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to load services."));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    const closeMenus = (event: MouseEvent) => {
      if (
        serviceMenuRef.current &&
        !serviceMenuRef.current.contains(event.target as Node)
      ) {
        setOpenServiceMenu(null);
        setMenuPosition(null);
      }
    };
    document.addEventListener("mousedown", closeMenus);
    return () => document.removeEventListener("mousedown", closeMenus);
  }, []);

  useEffect(() => {
    const closeFloatingServiceMenu = () => {
      setOpenServiceMenu(null);
      setMenuPosition(null);
    };
    window.addEventListener("scroll", closeFloatingServiceMenu, true);
    window.addEventListener("resize", closeFloatingServiceMenu);
    return () => {
      window.removeEventListener("scroll", closeFloatingServiceMenu, true);
      window.removeEventListener("resize", closeFloatingServiceMenu);
    };
  }, []);

  useEffect(() => {
    setSelectedIds(new Set());
  }, [filter, search]);

  const parseImport = async (content: string) => {
    setImportError(null);
    let rows: string[][];
    try {
      rows = parseCsv(content);
    } catch (err: unknown) {
      setImportRows([]);
      setImportError(err instanceof Error ? err.message : "Malformed CSV file");
      return;
    }
    if (rows.length < 2) {
      setImportRows([]);
      setImportError(
        "The CSV must include a header and at least one service row.",
      );
      return;
    }
    const headers = rows[0];
    const nameIndex = headerIndex(headers, ["name", "service", "servicename"]);
    const categoryIndex = headerIndex(headers, [
      "category",
      "categoryname",
      "type",
    ]);
    const durationIndex = headerIndex(headers, [
      "duration",
      "minutes",
      "durationminutes",
    ]);
    const priceIndex = headerIndex(headers, ["price", "cost"]);
    const descriptionIndex = headerIndex(headers, ["description", "details"]);
    const statusIndex = headerIndex(headers, ["status", "active"]);
    if (
      [nameIndex, categoryIndex, durationIndex, priceIndex].some(
        (index) => index < 0,
      )
    ) {
      setImportRows([]);
      setImportError(
        "Required columns are name, category, duration, and price.",
      );
      return;
    }

    const categoryByName = new Map(
      categories.map((category) => [
        category.name.trim().toLowerCase(),
        category,
      ]),
    );
    const allServices = filter ? await fetchManagedServices() : services;
    const existingKeys = new Set(
      allServices.map((service) =>
        duplicateKey({
          name: service.name,
          categoryId: service.category_id,
          duration: service.duration_minutes,
          price: service.price ?? "",
        }),
      ),
    );
    const seenKeys = new Set<string>();
    const parsed = rows.slice(1).map((values, offset): ImportRow => {
      const name = csvCell(values[nameIndex] ?? "");
      const categoryName = csvCell(values[categoryIndex] ?? "");
      const durationText = csvCell(values[durationIndex] ?? "");
      const price = csvCell(values[priceIndex] ?? "");
      const category = categoryByName.get(categoryName.toLowerCase());
      const durationValue = Number(durationText);
      const duration = Number.isInteger(durationValue) ? durationValue : null;
      const errors: string[] = [];
      if (!name) errors.push("Service name is required");
      if (!categoryName) errors.push("Category is required");
      if (!durationText || duration === null || duration < 5 || duration > 600)
        errors.push("Duration must be a whole number from 5 to 600");
      if (!price || !Number.isFinite(Number(price)) || Number(price) < 0)
        errors.push("Price must be a number of 0 or more");
      if (!categoryName || !category) errors.push("Category not found");
      const categoryId = category?.id ?? "";
      const key = duplicateKey({ name, categoryId, duration, price });
      const duplicate = Boolean(
        category && (existingKeys.has(key) || seenKeys.has(key)),
      );
      if (duplicate) errors.push("Already exists");
      if (!errors.length) seenKeys.add(key);
      return {
        line: offset + 2,
        name,
        categoryName,
        categoryId,
        duration,
        price,
        description: csvCell(values[descriptionIndex] ?? ""),
        isActive:
          statusIndex < 0 ||
          importStatus(csvCell(values[statusIndex] ?? "Active")),
        error: errors.length ? errors.join("; ") : null,
        duplicate,
        action: duplicate || !category ? "skip" : "import",
      };
    });
    setImportRows(parsed);
  };

  const handleImportFile = async (file: File | undefined) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setImportError("Please choose a CSV file (.csv).");
      return;
    }
    await parseImport(await file.text());
  };

  const downloadTemplate = () => {
    const blob = new Blob(
      [
        "name,category,duration,price,description,status\nDeep Cleansing Facial,Facial,60,120,Deep cleansing treatment,Active\n",
      ],
      { type: "text/csv" },
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "services-template.csv";
    link.click();
    URL.revokeObjectURL(url);
  };

  const updateImportRow = (line: number, update: Partial<ImportRow>) => {
    setImportRows((rows) =>
      rows.map((row) => (row.line === line ? { ...row, ...update } : row)),
    );
  };

  const importReadyRows = importRows.filter((row) => {
    if (!row.error) return row.action === "import";
    if (row.error.includes("Already exists")) return row.action === "import";
    if (row.error.includes("Category not found"))
      return row.action === "import" || row.action === "create";
    return false;
  });
  const importBlockedRows = importRows.filter(
    (row) => row.error && row.action !== "import",
  );

  const confirmImport = async () => {
    if (!importReadyRows.length) return;
    setImporting(true);
    setImportError(null);
    let imported = 0;
    const failures: string[] = [];
    try {
      for (const row of importReadyRows) {
        let categoryId = row.categoryId;
        if (row.action === "create") {
          const created = await createServiceCategory({
            name: row.categoryName,
          });
          categoryId = created.id;
        }
        try {
          await createManagedService({
            name: row.name,
            category_id: categoryId,
            description: row.description || null,
            price: row.price,
            duration_minutes: row.duration ?? 60,
            is_active: row.isActive,
          });
          imported += 1;
        } catch (err: unknown) {
          failures.push(
            `Line ${row.line}: ${getApiErrorMessage(err, "Unable to import row")}`,
          );
        }
      }
      await load();
      setImportOpen(false);
      setImportRows([]);
      if (failures.length)
        setError(
          `${imported} services imported; ${failures.length} rows failed. ${failures.join(" ")}`,
        );
    } finally {
      setImporting(false);
    }
  };

  const saveService = async () => {
    if (!editing || !editing.name.trim() || !editing.category_id) return;
    setBusy(true);
    try {
      if (editingId) await updateManagedService(editingId, editing);
      else await createManagedService(editing);
      setEditing(null);
      setEditingId(null);
      await load();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to save service."));
    } finally {
      setBusy(false);
    }
  };

  const saveCategory = async () => {
    if (!categoryName.trim()) return;
    setBusy(true);
    try {
      if (categoryEditId)
        await updateServiceCategory(categoryEditId, {
          name: categoryName.trim(),
        });
      else await createServiceCategory({ name: categoryName.trim() });
      setCategoryName("");
      setCategoryEditId(null);
      setCategoryModal(null);
      await load();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to save category."));
    } finally {
      setBusy(false);
    }
  };

  const removeCategory = async (category: ServiceCategory) => {
    if (category.service_count > 0) {
      setError(
        "Move or delete the services in this category before deleting it.",
      );
      return;
    }
    if (!window.confirm(`Delete ${category.name}?`)) return;
    setBusy(true);
    try {
      await deleteServiceCategory(category.id);
      await load();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to delete category."));
    } finally {
      setBusy(false);
    }
  };

  const removeService = async (service: ManagedService) => {
    setDeleteTarget([service]);
  };

  const runDelete = async () => {
    if (!deleteTarget?.length) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await bulkDeleteManagedServices({
        service_ids: deleteTarget.map((service) => service.id),
      });
      setDeleteTarget(null);
      setSelectedIds(new Set());
      setSuccess(
        `${result.deleted} service${result.deleted === 1 ? "" : "s"} deleted successfully.`,
      );
      await load();
    } catch (err: unknown) {
      setError(
        getApiErrorMessage(err, "Unable to delete selected service(s)."),
      );
    } finally {
      setBusy(false);
    }
  };

  const runDeleteAll = async () => {
    if (deleteAllText !== "DELETE ALL") return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await bulkDeleteManagedServices({ delete_all: true });
      setDeleteAllOpen(false);
      setDeleteAllText("");
      setSelectedIds(new Set());
      setSuccess(`${result.deleted} services deleted successfully.`);
      await load();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to delete all services."));
    } finally {
      setBusy(false);
    }
  };

  const runBulkStatus = async (isActive: boolean) => {
    if (!selectedServices.length) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await Promise.all(
        selectedServices.map((service) =>
          updateManagedService(service.id, {
            category_id: service.category_id,
            name: service.name,
            description: service.description,
            price: service.price,
            duration_minutes: service.duration_minutes,
            is_active: isActive,
          }),
        ),
      );
      setSelectedIds(new Set());
      setSuccess(
        `${selectedServices.length} service${selectedServices.length === 1 ? "" : "s"} ${isActive ? "activated" : "deactivated"} successfully.`,
      );
      await load();
    } catch (err: unknown) {
      setError(
        getApiErrorMessage(
          err,
          `Unable to ${isActive ? "activate" : "deactivate"} selected services.`,
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const openAddService = (categoryId = filter || categories[0]?.id || "") => {
    setEditing(emptyService(categoryId));
    setEditingId(null);
  };

  const normalizedSearch = search.trim().toLowerCase();
  const visibleServices = services.filter(
    (service) =>
      (!filter || service.category_id === filter) &&
      service.name.toLowerCase().includes(normalizedSearch),
  );
  const visibleIds = visibleServices.map((service) => service.id);
  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));
  const selectedServices = services.filter((service) =>
    selectedIds.has(service.id),
  );

  const toggleSelected = (id: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllVisible = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) visibleIds.forEach((id) => next.delete(id));
      else visibleIds.forEach((id) => next.add(id));
      return next;
    });
  };

  const openServiceActions = (
    service: ManagedService,
    button: HTMLButtonElement,
  ) => {
    const rect = button.getBoundingClientRect();
    const menuHeight = 100;
    setOpenServiceMenu(openServiceMenu === service.id ? null : service.id);
    setMenuPosition({
      top:
        rect.bottom + menuHeight > window.innerHeight
          ? rect.top - menuHeight
          : rect.bottom + 4,
      left: Math.max(8, rect.right - 140),
    });
    serviceMenuRef.current = button;
  };

  const beginCategoryEdit = (category: ServiceCategory) => {
    setCategoryEditId(category.id);
    setCategoryName(category.name);
    setCategoryModal("edit");
    setOpenCategoryMenu(null);
  };

  const openMenuService = services.find(
    (service) => service.id === openServiceMenu,
  );

  return (
    <>
      <PageHeader
        eyebrow="Spa Receptionist"
        title="Services"
        subtitle="Organize the menu your receptionist presents and books."
        actions={
          editable ? (
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => {
                  setImportOpen(true);
                  setImportRows([]);
                  setImportError(null);
                }}
                className="inline-flex items-center gap-2 rounded-xl border border-slate-700 px-4 py-2.5 text-xs font-bold text-slate-300 hover:border-cyan-400/50 hover:text-cyan-300"
              >
                <FileUp size={15} /> Import CSV
              </button>
              <button
                onClick={() => openAddService()}
                className="inline-flex items-center gap-2 rounded-xl bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950"
              >
                <Plus size={15} /> Add service
              </button>
              <button
                onClick={() => {
                  setDeleteAllText("");
                  setDeleteAllOpen(true);
                }}
                disabled={busy || services.length === 0}
                className="inline-flex items-center gap-2 rounded-xl border border-rose-400/30 px-4 py-2.5 text-xs font-bold text-rose-300 hover:bg-rose-400/5 disabled:cursor-not-allowed disabled:opacity-40"
                title="Delete all services"
              >
                <Trash2 size={15} /> Delete all
              </button>
            </div>
          ) : undefined
        }
      />
      {success && (
        <div className="flex items-center justify-between rounded-xl border border-emerald-400/20 bg-emerald-400/5 px-3 py-2.5 text-xs text-emerald-300">
          <span>{success}</span>
          <button onClick={() => setSuccess(null)} aria-label="Dismiss success">
            <X size={14} />
          </button>
        </div>
      )}
      <div className="flex items-center gap-1 border-b border-slate-800">
        {(["all", "categories"] as const).map((value) => (
          <button
            key={value}
            onClick={() => setTab(value)}
            className={`border-b-2 px-4 py-3 text-xs font-semibold capitalize ${tab === value ? "border-cyan-400 text-cyan-300" : "border-transparent text-slate-500"}`}
          >
            {value === "all" ? "All services" : "Categories"}
          </button>
        ))}
        <button
          onClick={() => void load()}
          disabled={loading || busy}
          className="ml-auto p-2 text-slate-500 hover:text-cyan-300"
          aria-label="Refresh services"
        >
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
        </button>
      </div>
      <StateBlock loading={loading} error={error}>
        {tab === "all" ? (
          <Panel
            title="All services"
            subtitle={
              filter || search.trim()
                ? `${visibleServices.length} of ${services.length} services`
                : `${services.length} service${services.length === 1 ? "" : "s"}`
            }
            padded={false}
          >
            <div className="flex flex-col gap-2 border-b border-slate-800 p-4 sm:flex-row">
              <label className="relative flex-1">
                <Search
                  size={14}
                  className="absolute left-3 top-2.5 text-slate-500"
                />
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search services..."
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] py-2 pl-9 pr-3 text-xs text-slate-200 outline-none placeholder:text-slate-600"
                />
              </label>
              <select
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200"
              >
                <option value="">All categories</option>
                {categories.map((category) => (
                  <option key={category.id} value={category.id}>
                    {category.name}
                  </option>
                ))}
              </select>
            </div>
            {selectedIds.size > 0 && (
              <div className="flex flex-wrap items-center gap-2 border-b border-cyan-400/20 bg-cyan-400/[.04] px-4 py-3">
                <span className="mr-2 text-xs font-semibold text-cyan-200">
                  {selectedIds.size} service{selectedIds.size === 1 ? "" : "s"}{" "}
                  selected
                </span>
                <button
                  onClick={() => void runBulkStatus(true)}
                  disabled={busy}
                  className="rounded-lg border border-emerald-400/30 px-3 py-1.5 text-[11px] font-semibold text-emerald-300 disabled:opacity-50"
                >
                  Activate
                </button>
                <button
                  onClick={() => void runBulkStatus(false)}
                  disabled={busy}
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-[11px] font-semibold text-slate-300 disabled:opacity-50"
                >
                  Deactivate
                </button>
                <button
                  onClick={() => setDeleteTarget(selectedServices)}
                  disabled={busy}
                  className="rounded-lg border border-rose-400/30 px-3 py-1.5 text-[11px] font-semibold text-rose-300 disabled:opacity-50"
                >
                  Delete Selected
                </button>
              </div>
            )}
            <div className="grid grid-cols-[2rem_minmax(0,1.5fr)_minmax(8rem,1fr)_7rem_7rem_6rem_7rem] gap-3 border-b border-slate-800 px-4 py-3 text-[10px] font-bold uppercase tracking-wide text-slate-600">
              <span>
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  onChange={toggleAllVisible}
                  aria-label="Select all visible services"
                />
              </span>
              <span>Service</span>
              <span>Category</span>
              <span>Duration</span>
              <span>Price</span>
              <span>Status</span>
              <span>Actions</span>
            </div>
            <div className="divide-y divide-slate-800">
              {visibleServices.map((service) => (
                <div
                  key={service.id}
                  className="grid gap-2 p-4 md:grid-cols-[2rem_minmax(0,1.5fr)_minmax(8rem,1fr)_7rem_7rem_6rem_7rem] md:items-center"
                >
                  <div>
                    <input
                      type="checkbox"
                      checked={selectedIds.has(service.id)}
                      onChange={() => toggleSelected(service.id)}
                      aria-label={`Select ${service.name}`}
                    />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-slate-200">
                      {service.name}
                    </p>
                  </div>
                  <span className="text-xs text-slate-400 md:text-slate-300">
                    {service.category_name}
                  </span>
                  <span className="text-xs text-slate-300">
                    {service.duration_minutes} min
                  </span>
                  <span className="text-xs text-slate-300">
                    {displayPrice(service.price)}
                  </span>
                  <span
                    className={`w-fit rounded-full px-2 py-1 text-[10px] ${service.is_active ? "bg-emerald-400/10 text-emerald-300" : "bg-slate-700 text-slate-400"}`}
                  >
                    {service.is_active ? "Active" : "Inactive"}
                  </span>
                  <div className="relative flex items-center gap-1">
                    <button
                      onClick={() => {
                        setEditing({
                          category_id: service.category_id,
                          name: service.name,
                          description: service.description,
                          price: service.price,
                          duration_minutes: service.duration_minutes,
                          is_active: service.is_active,
                        });
                        setEditingId(service.id);
                      }}
                      className="p-2 text-slate-500 hover:text-cyan-300"
                      aria-label={`Edit ${service.name}`}
                      title="Edit"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      onClick={() => void removeService(service)}
                      className="p-2 text-slate-500 hover:text-rose-300"
                      aria-label={`Delete ${service.name}`}
                      title="Delete service"
                    >
                      <Trash2 size={14} />
                    </button>
                    <button
                      onClick={(event) =>
                        openServiceActions(service, event.currentTarget)
                      }
                      className="p-2 text-slate-500 hover:text-cyan-300"
                      aria-label={`More actions for ${service.name}`}
                      title="More actions"
                    >
                      <MoreHorizontal size={15} />
                    </button>
                  </div>
                </div>
              ))}
              {!visibleServices.length && (
                <div className="p-6 text-xs text-slate-500">
                  {filter
                    ? "No services in this category yet."
                    : "No services match your search."}
                  {filter && editable && (
                    <button
                      onClick={() => openAddService(filter)}
                      className="ml-3 font-semibold text-cyan-300 hover:text-cyan-200"
                    >
                      + Add service
                    </button>
                  )}
                </div>
              )}
            </div>
          </Panel>
        ) : (
          <Panel
            title="Categories"
            subtitle="Select a category to filter its services."
            padded={false}
          >
            <div className="flex justify-end border-b border-slate-800 p-4">
              {editable && (
                <button
                  onClick={() => {
                    setCategoryName("");
                    setCategoryEditId(null);
                    setCategoryModal("add");
                  }}
                  className="inline-flex items-center gap-2 rounded-lg border border-cyan-400/40 px-3 py-2 text-[11px] font-semibold text-cyan-300 hover:bg-cyan-400/5"
                >
                  <Plus size={14} /> Add Category
                </button>
              )}
            </div>
            <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
              {categories.map((category) => (
                <div
                  key={category.id}
                  className={`relative rounded-xl border p-4 ${category.is_active ? "border-slate-800 bg-[#091525]" : "border-slate-800/70 bg-slate-900/50"}`}
                >
                  <button
                    onClick={() => {
                      setFilter(category.id);
                      setSearch("");
                      setTab("all");
                    }}
                    className="block w-full text-left"
                  >
                    <div className="flex items-center gap-2">
                      <p
                        className={`font-semibold ${category.is_active ? "text-slate-200" : "text-slate-500"}`}
                      >
                        {category.name}
                      </p>
                      {!category.is_active && (
                        <span className="rounded-full bg-slate-700 px-2 py-1 text-[9px] text-slate-400">
                          Inactive
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-slate-500">
                      {category.service_count} service
                      {category.service_count === 1 ? "" : "s"}
                    </p>
                  </button>
                  <div className="mt-3 flex items-center justify-between">
                    <button
                      onClick={() => {
                        setFilter(category.id);
                        setSearch("");
                        setTab("all");
                      }}
                      className="text-[11px] font-semibold text-cyan-300 hover:text-cyan-200"
                    >
                      View services
                    </button>
                    {editable && (
                      <div className="relative">
                        <button
                          onClick={() =>
                            setOpenCategoryMenu(
                              openCategoryMenu === category.id
                                ? null
                                : category.id,
                            )
                          }
                          className="p-1 text-slate-500 hover:text-cyan-300"
                          aria-label={`More actions for ${category.name}`}
                          title="More actions"
                        >
                          <MoreHorizontal size={16} />
                        </button>
                        {openCategoryMenu === category.id && (
                          <div className="absolute right-0 top-8 z-10 w-36 rounded-lg border border-slate-700 bg-[#091525] p-1 shadow-xl">
                            <button
                              onClick={() => beginCategoryEdit(category)}
                              className="w-full rounded px-3 py-2 text-left text-[11px] text-slate-300 hover:bg-slate-800"
                            >
                              Rename
                            </button>
                            <button
                              onClick={() => {
                                setOpenCategoryMenu(null);
                                void updateServiceCategory(category.id, {
                                  name: category.name,
                                  is_active: !category.is_active,
                                }).then(load);
                              }}
                              className="w-full rounded px-3 py-2 text-left text-[11px] text-slate-300 hover:bg-slate-800"
                            >
                              {category.is_active ? "Deactivate" : "Activate"}
                            </button>
                            <button
                              onClick={() => {
                                setOpenCategoryMenu(null);
                                void removeCategory(category);
                              }}
                              className="w-full rounded px-3 py-2 text-left text-[11px] text-rose-300 hover:bg-slate-800"
                            >
                              Delete
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        )}
      </StateBlock>
      {openMenuService &&
        menuPosition &&
        createPortal(
          <div
            onMouseDown={(event) => event.stopPropagation()}
            style={{ top: menuPosition.top, left: menuPosition.left }}
            className="fixed z-[100] w-40 rounded-lg border border-slate-700 bg-[#091525] p-1 shadow-2xl"
          >
            <button
              onClick={() => {
                setOpenServiceMenu(null);
                setMenuPosition(null);
                setEditing({
                  category_id: openMenuService.category_id,
                  name: openMenuService.name,
                  description: openMenuService.description,
                  price: openMenuService.price,
                  duration_minutes: openMenuService.duration_minutes,
                  is_active: openMenuService.is_active,
                });
                setEditingId(openMenuService.id);
              }}
              className="w-full rounded px-3 py-2 text-left text-[11px] text-slate-300 hover:bg-slate-800"
            >
              Edit Service
            </button>
            <button
              onClick={() => {
                setOpenServiceMenu(null);
                setMenuPosition(null);
                void updateManagedService(openMenuService.id, {
                  category_id: openMenuService.category_id,
                  name: openMenuService.name,
                  description: openMenuService.description,
                  price: openMenuService.price,
                  duration_minutes: openMenuService.duration_minutes,
                  is_active: !openMenuService.is_active,
                }).then(load);
              }}
              className="w-full rounded px-3 py-2 text-left text-[11px] text-slate-300 hover:bg-slate-800"
            >
              {openMenuService.is_active
                ? "Deactivate Service"
                : "Activate Service"}
            </button>
            <button
              onClick={() => {
                setOpenServiceMenu(null);
                setMenuPosition(null);
                void removeService(openMenuService);
              }}
              className="w-full rounded px-3 py-2 text-left text-[11px] text-rose-300 hover:bg-slate-800"
            >
              Delete Service
            </button>
          </div>,
          document.body,
        )}
      {deleteTarget && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-slate-950/75 p-4">
          <div className="w-full max-w-md rounded-2xl border border-slate-700 bg-[#091525] p-5">
            <h2 className="font-semibold text-white">
              {deleteTarget.length === 1
                ? "Delete service?"
                : `Delete ${deleteTarget.length} services?`}
            </h2>
            <p className="mt-2 text-xs leading-relaxed text-slate-400">
              {deleteTarget.length === 1
                ? "You are about to permanently delete:"
                : "The following services will be permanently deleted:"}
            </p>
            <div className="mt-3 max-h-40 space-y-1 overflow-y-auto rounded-lg border border-slate-800 bg-[#07111f] p-3 text-xs text-slate-200">
              {deleteTarget.map((service) => (
                <div key={service.id}>
                  {service.name} — {service.duration_minutes} min —{" "}
                  {displayPrice(service.price)}
                </div>
              ))}
            </div>
            <p className="mt-3 text-[11px] text-rose-300">
              This action cannot be undone.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setDeleteTarget(null)}
                className="rounded-lg border border-slate-700 px-4 py-2.5 text-xs font-semibold text-slate-300"
              >
                Cancel
              </button>
              <button
                onClick={() => void runDelete()}
                disabled={busy}
                className="inline-flex items-center gap-2 rounded-lg bg-rose-500 px-4 py-2.5 text-xs font-bold text-white disabled:opacity-50"
              >
                <Trash2 size={14} /> Delete{" "}
                {deleteTarget.length === 1
                  ? "Service"
                  : `${deleteTarget.length} Services`}
              </button>
            </div>
          </div>
        </div>
      )}
      {deleteAllOpen && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-slate-950/75 p-4">
          <div className="w-full max-w-md rounded-2xl border border-rose-400/30 bg-[#091525] p-5">
            <h2 className="font-semibold text-white">Delete all services?</h2>
            <p className="mt-2 text-xs leading-relaxed text-slate-400">
              This will permanently delete all{" "}
              <strong className="text-white">{services.length} services</strong>{" "}
              from your Services menu. Categories will not be deleted.
            </p>
            <p className="mt-4 text-xs text-slate-300">
              To continue, type{" "}
              <strong className="text-rose-300">DELETE ALL</strong>
            </p>
            <input
              aria-label="Type DELETE ALL to confirm"
              value={deleteAllText}
              onChange={(event) => setDeleteAllText(event.target.value)}
              className="mt-2 w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => {
                  setDeleteAllOpen(false);
                  setDeleteAllText("");
                }}
                className="rounded-lg border border-slate-700 px-4 py-2.5 text-xs font-semibold text-slate-300"
              >
                Cancel
              </button>
              <button
                onClick={() => void runDeleteAll()}
                disabled={deleteAllText !== "DELETE ALL" || busy}
                className="rounded-lg bg-rose-500 px-4 py-2.5 text-xs font-bold text-white disabled:opacity-40"
              >
                Delete All Services
              </button>
            </div>
          </div>
        </div>
      )}
      {categoryModal && (
        <div className="fixed inset-0 z-30 grid place-items-center bg-slate-950/70 p-4">
          <div className="w-full max-w-md rounded-2xl border border-slate-700 bg-[#091525] p-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-semibold text-white">
                {categoryModal === "edit" ? "Rename Category" : "Add Category"}
              </h2>
              <button
                onClick={() => setCategoryModal(null)}
                className="text-slate-500"
              >
                <X size={18} />
              </button>
            </div>
            <label className="block">
              <span className="mb-2 block text-xs font-medium text-slate-400">
                Category Name
              </span>
              <input
                autoFocus
                value={categoryName}
                onChange={(event) => setCategoryName(event.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none"
              />
            </label>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setCategoryModal(null)}
                className="rounded-lg border border-slate-700 px-4 py-2.5 text-xs font-semibold text-slate-300"
              >
                Cancel
              </button>
              <button
                onClick={() => void saveCategory()}
                disabled={busy || !categoryName.trim()}
                className="rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 disabled:opacity-50"
              >
                {categoryModal === "edit" ? "Save Category" : "Create Category"}
              </button>
            </div>
          </div>
        </div>
      )}
      {importOpen && (
        <div className="fixed inset-0 z-30 grid place-items-center bg-slate-950/70 p-4">
          <div className="max-h-[90vh] w-full max-w-5xl overflow-y-auto rounded-2xl border border-slate-700 bg-[#091525] p-5">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="font-semibold text-white">
                  Import services from CSV
                </h2>
                <p className="mt-1 text-[11px] text-slate-500">
                  Preview validates rows before anything is saved.
                </p>
              </div>
              <button
                onClick={() => setImportOpen(false)}
                className="text-slate-500"
              >
                <X size={18} />
              </button>
            </div>
            <input
              ref={importInputRef}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(event) => {
                void handleImportFile(event.target.files?.[0]);
                event.target.value = "";
              }}
            />
            <div
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                void handleImportFile(event.dataTransfer.files[0]);
              }}
              onClick={() => importInputRef.current?.click()}
              className="cursor-pointer rounded-xl border border-dashed border-slate-700 px-5 py-6 text-center hover:border-cyan-400/50"
            >
              <Upload className="mx-auto mb-2 text-cyan-300" size={20} />
              <p className="text-xs font-semibold text-slate-300">
                Drop a CSV here or choose a file
              </p>
              <p className="mt-1 text-[10px] text-slate-500">
                Required columns: name, category, duration, price
              </p>
            </div>
            <div className="mt-3 flex justify-end">
              <button
                onClick={downloadTemplate}
                className="inline-flex items-center gap-2 text-[11px] font-semibold text-cyan-300 hover:text-cyan-200"
              >
                <FileUp size={14} /> Download CSV Template
              </button>
            </div>
            {importError && (
              <p className="mt-3 rounded-lg border border-rose-400/20 bg-rose-400/5 px-3 py-2 text-[11px] text-rose-300">
                {importError}
              </p>
            )}
            {importRows.length > 0 && (
              <>
                <div className="mt-4 overflow-x-auto rounded-lg border border-slate-800">
                  <table className="w-full text-left text-[11px]">
                    <thead className="border-b border-slate-800 text-slate-500">
                      <tr>
                        <th className="p-3">Line</th>
                        <th className="p-3">Service</th>
                        <th className="p-3">Category</th>
                        <th className="p-3">Duration</th>
                        <th className="p-3">Price</th>
                        <th className="p-3">Status</th>
                        <th className="p-3">Result</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800">
                      {importRows.map((row) => (
                        <tr key={row.line}>
                          <td className="p-3 text-slate-500">{row.line}</td>
                          <td className="p-3 text-slate-200">
                            {row.name || "—"}
                          </td>
                          <td className="p-3">
                            <div className="flex min-w-44 gap-2">
                              <span className="text-slate-300">
                                {row.categoryName || "—"}
                              </span>
                              {row.error?.includes("Category not found") && (
                                <select
                                  value={
                                    row.action === "create"
                                      ? "create"
                                      : row.categoryId
                                        ? "map"
                                        : "skip"
                                  }
                                  onChange={(event) => {
                                    const value = event.target.value as
                                      "skip" | "map" | "create";
                                    updateImportRow(row.line, {
                                      action:
                                        value === "map" ? "import" : value,
                                      categoryId:
                                        value === "map"
                                          ? (categories[0]?.id ?? "")
                                          : "",
                                    });
                                  }}
                                  className="rounded border border-slate-700 bg-[#07111f] px-1 py-1 text-[10px] text-slate-200"
                                >
                                  <option value="skip">Skip</option>
                                  <option value="map">Map</option>
                                  <option value="create">Create</option>
                                </select>
                              )}
                              {row.error?.includes("Category not found") &&
                                row.action === "import" && (
                                  <select
                                    value={row.categoryId}
                                    onChange={(event) =>
                                      updateImportRow(row.line, {
                                        categoryId: event.target.value,
                                      })
                                    }
                                    className="rounded border border-slate-700 bg-[#07111f] px-1 py-1 text-[10px] text-slate-200"
                                  >
                                    {categories.map((category) => (
                                      <option
                                        key={category.id}
                                        value={category.id}
                                      >
                                        {category.name}
                                      </option>
                                    ))}
                                  </select>
                                )}
                            </div>
                          </td>
                          <td className="p-3 text-slate-300">
                            {row.duration ?? "—"}
                          </td>
                          <td className="p-3 text-slate-300">
                            {row.price || "—"}
                          </td>
                          <td className="p-3 text-slate-300">
                            {row.isActive ? "Active" : "Inactive"}
                          </td>
                          <td
                            className={`p-3 ${row.error && row.action !== "import" && row.action !== "create" ? "text-rose-300" : "text-emerald-300"}`}
                          >
                            {row.error || "Ready"}
                            {row.duplicate && (
                              <select
                                value={row.action}
                                onChange={(event) =>
                                  updateImportRow(row.line, {
                                    action: event.target.value as ImportAction,
                                  })
                                }
                                className="ml-2 rounded border border-slate-700 bg-[#07111f] px-1 py-1 text-[10px] text-slate-200"
                              >
                                <option value="skip">Skip duplicate</option>
                                <option value="import">Import anyway</option>
                              </select>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                  <div className="text-[11px] text-slate-400">
                    Ready to import:{" "}
                    <strong className="text-emerald-300">
                      {importReadyRows.length}
                    </strong>{" "}
                    · Duplicates skipped:{" "}
                    <strong>
                      {
                        importRows.filter(
                          (row) => row.duplicate && row.action === "skip",
                        ).length
                      }
                    </strong>{" "}
                    · Rows with errors:{" "}
                    <strong className="text-rose-300">
                      {importBlockedRows.length}
                    </strong>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setImportOpen(false)}
                      className="rounded-lg border border-slate-700 px-4 py-2 text-xs font-semibold text-slate-300"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => void confirmImport()}
                      disabled={importing || !importReadyRows.length}
                      className="rounded-lg bg-cyan-400 px-4 py-2 text-xs font-bold text-slate-950 disabled:opacity-50"
                    >
                      {importing
                        ? "Importing…"
                        : `Import ${importReadyRows.length} Services`}
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
      {editing && (
        <div className="fixed inset-0 z-30 grid place-items-center bg-slate-950/70 p-4">
          <div className="w-full max-w-lg rounded-2xl border border-slate-700 bg-[#091525] p-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-semibold text-white">
                {editingId ? "Edit service" : "Add service"}
              </h2>
              <button
                onClick={() => setEditing(null)}
                className="text-slate-500"
              >
                <X size={18} />
              </button>
            </div>
            <div className="grid gap-3">
              <input
                value={editing.name}
                onChange={(event) =>
                  setEditing({ ...editing, name: event.target.value })
                }
                placeholder="Service name"
                className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white"
              />
              <select
                value={editing.category_id}
                onChange={(event) =>
                  setEditing({ ...editing, category_id: event.target.value })
                }
                className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white"
              >
                <option value="">Choose category</option>
                {categories.map((category) => (
                  <option key={category.id} value={category.id}>
                    {category.name}
                  </option>
                ))}
              </select>
              <div className="grid grid-cols-2 gap-3">
                <input
                  type="number"
                  min="5"
                  value={editing.duration_minutes}
                  onChange={(event) =>
                    setEditing({
                      ...editing,
                      duration_minutes: Number(event.target.value),
                    })
                  }
                  placeholder="Duration"
                  className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white"
                />
                <input
                  value={editing.price ?? ""}
                  onChange={(event) =>
                    setEditing({ ...editing, price: event.target.value })
                  }
                  placeholder="Price"
                  className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white"
                />
              </div>
              <textarea
                value={editing.description ?? ""}
                onChange={(event) =>
                  setEditing({ ...editing, description: event.target.value })
                }
                placeholder="Description"
                rows={3}
                className="rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white"
              />
              <label className="flex items-center gap-2 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={editing.is_active}
                  onChange={(event) =>
                    setEditing({ ...editing, is_active: event.target.checked })
                  }
                />{" "}
                Active
              </label>
              <button
                onClick={() => void saveService()}
                disabled={busy || !editing.name.trim() || !editing.category_id}
                className="rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 disabled:opacity-50"
              >
                Save service
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
