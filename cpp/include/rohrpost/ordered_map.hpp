// A small insertion-ordered map, standing in for Python's dict where the
// reference implementation relies on insertion order (ticket fold order,
// per-field timestamps, remote bindings, event payloads).
#pragma once

#include <cstddef>
#include <initializer_list>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace rp {

template <class Key, class Value>
class OrderedMap {
public:
    using value_type = std::pair<Key, Value>;
    using iterator = typename std::vector<value_type>::iterator;
    using const_iterator = typename std::vector<value_type>::const_iterator;

    OrderedMap() = default;
    OrderedMap(std::initializer_list<value_type> items) {
        for (auto& item : items) insert_or_assign(item.first, item.second);
    }

    /// Insert or overwrite; an existing key keeps its position (dict semantics).
    Value& insert_or_assign(const Key& key, Value value) {
        auto it = index_.find(key);
        if (it != index_.end()) {
            items_[it->second].second = std::move(value);
            return items_[it->second].second;
        }
        index_.emplace(key, items_.size());
        items_.emplace_back(key, std::move(value));
        return items_.back().second;
    }

    Value& operator[](const Key& key) {
        auto it = index_.find(key);
        if (it != index_.end()) return items_[it->second].second;
        return insert_or_assign(key, Value{});
    }

    [[nodiscard]] const Value* find(const Key& key) const {
        auto it = index_.find(key);
        if (it == index_.end()) return nullptr;
        return &items_[it->second].second;
    }
    [[nodiscard]] Value* find(const Key& key) {
        auto it = index_.find(key);
        if (it == index_.end()) return nullptr;
        return &items_[it->second].second;
    }
    [[nodiscard]] bool contains(const Key& key) const { return index_.contains(key); }

    /// Remove a key (dict.pop). Returns whether it was present.
    bool erase(const Key& key) {
        auto it = index_.find(key);
        if (it == index_.end()) return false;
        const std::size_t pos = it->second;
        items_.erase(items_.begin() + static_cast<std::ptrdiff_t>(pos));
        index_.erase(it);
        for (auto& [k, i] : index_) {
            if (i > pos) --i;
        }
        return true;
    }

    [[nodiscard]] std::size_t size() const { return items_.size(); }
    [[nodiscard]] bool empty() const { return items_.empty(); }
    void clear() { items_.clear(); index_.clear(); }

    iterator begin() { return items_.begin(); }
    iterator end() { return items_.end(); }
    const_iterator begin() const { return items_.begin(); }
    const_iterator end() const { return items_.end(); }
    const std::vector<value_type>& items() const { return items_; }

    [[nodiscard]] std::vector<Key> keys() const {
        std::vector<Key> out;
        out.reserve(items_.size());
        for (const auto& [k, v] : items_) out.push_back(k);
        return out;
    }

    /// Order-insensitive equality, matching Python dict equality.
    bool operator==(const OrderedMap& other) const {
        if (size() != other.size()) return false;
        for (const auto& [k, v] : items_) {
            const Value* ov = other.find(k);
            if (ov == nullptr || !(*ov == v)) return false;
        }
        return true;
    }

private:
    std::vector<value_type> items_;
    std::unordered_map<Key, std::size_t> index_;
};

}  // namespace rp
