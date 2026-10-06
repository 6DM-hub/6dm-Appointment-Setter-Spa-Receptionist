import React, { useState } from "react";
import { UserPlus, User, Phone, Mail } from "lucide-react";
import { type Contact, createContact } from "../api/client";

interface ContactManagerProps {
  contacts: Contact[];
  onContactAdded: () => void;
}

export const ContactManager: React.FC<ContactManagerProps> = ({ contacts, onContactAdded }) => {
  const [fullName, setFullName] = useState("");
  const [phoneNumber, setPhoneNumber] = useState("");
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!fullName || !phoneNumber) return;

    setLoading(true);
    setError(null);
    try {
      // ContactCreate takes first_name/last_name; `full_name` is a read-only
      // derived field, so posting it would have been silently discarded.
      const [firstName, ...rest] = fullName.trim().split(/\s+/);
      await createContact({
        first_name: firstName,
        last_name: rest.join(" ") || null,
        phone_number: phoneNumber,
        email: email || null,
      });
      setFullName("");
      setPhoneNumber("");
      setEmail("");
      onContactAdded();
    } catch (err: any) {
      console.error("Failed to add contact", err);
      const detail = err?.response?.data?.detail;
      setError(
        typeof detail === "string"
          ? detail
          : err?.response?.status === 401
            ? "Please log in before adding contacts."
            : "Failed to add contact."
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
      {/* Create Contact Form */}
      <div className="bg-slate-900 border border-slate-800 p-6 rounded-xl">
        <h3 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
          <UserPlus className="w-5 h-5 text-indigo-400" /> New Contact
        </h3>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Full Name</label>
            <input
              type="text"
              required
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Jane Doe"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-sm text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Phone Number</label>
            <input
              type="text"
              required
              value={phoneNumber}
              onChange={(e) => setPhoneNumber(e.target.value)}
              placeholder="+15551234567"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-sm text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Email (Optional)</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="jane@example.com"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-sm text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-sm rounded-lg transition-colors disabled:opacity-50"
          >
            {loading ? "Adding..." : "Add Contact"}
          </button>
          {error && <p className="text-xs text-red-400">{error}</p>}
        </form>
      </div>

      {/* Contacts List */}
      <div className="md:col-span-2 bg-slate-900 border border-slate-800 p-6 rounded-xl">
        <h3 className="text-lg font-semibold text-white mb-4">Saved Contacts ({contacts.length})</h3>
        <div className="space-y-3 max-h-96 overflow-y-auto pr-1">
          {contacts.length === 0 ? (
            <p className="text-slate-500 text-sm">No contacts added yet.</p>
          ) : (
            contacts.map((contact) => (
              <div
                key={contact.id}
                className="flex items-center justify-between bg-slate-950 p-3.5 border border-slate-800 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 bg-slate-800 rounded-full flex items-center justify-center">
                    <User className="w-5 h-5 text-indigo-400" />
                  </div>
                  <div>
                    <h4 className="text-sm font-medium text-slate-200">{contact.full_name}</h4>
                    <div className="flex items-center gap-3 text-xs text-slate-400 mt-0.5">
                      <span className="flex items-center gap-1">
                        <Phone className="w-3 h-3" /> {contact.phone_number}
                      </span>
                      {contact.email && (
                        <span className="flex items-center gap-1">
                          <Mail className="w-3 h-3" /> {contact.email}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};